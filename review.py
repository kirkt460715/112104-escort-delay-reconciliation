"""行程复盘与窗口试算：联合排期会可重复使用的能力。

只读 data/ 下的交换表，不改变台账。仅用标准库。

子命令：
  journey     单次行程复盘：每段可确定与可能的接续延误、重叠关系、单改测试
  report      按同类流程与时段汇总：等待分布、脱落、归因支撑（可合计口径）
  whatif      窗口试算：移动接人窗 / 改报到截止 / 两边一起改，
              带入车辆跨程与连班陪诊员周转约束，守住锁定预约
  acceptance  核准复核：已核准方案自生效日起按真实窗口重判，未生效留作试用

归因口径：
  凭证是区间。汇合环节只有一方的就绪区间确定不早于其余各方时才算确定责任；
  先后无法辨认时保留不同归因成立的范围（contested），同一段分钟只计一次。
  家属认可暂停覆盖的分钟不记服务方超时；凭证缺失单列，不替任何方顶责。
  替补等待（到院至替补窗）属系统安排，不全摊入原窗口责任方。
"""
from __future__ import annotations

from argparse import ArgumentParser
from collections import defaultdict
import csv
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"

JOIN_STAGES = ("family", "car", "escort")
PARTY_NAME = {"family": "家属", "car": "接送队", "escort": "陪诊组"}
OUTCOME_NAME = {"served": "原窗口完成", "replacement": "替补完成",
                "no_replacement": "院方取消"}
MISSING_PARTY = "凭证缺失方"
PAUSE_PARTY = "家属暂停"
SYSTEM_PARTY = "替补系统"
HOSPITAL = "院方"


def fmt(minute):
    """业务日连续分钟 -> HH:MM；超过 1440 为次日，仍按行程连续计。"""
    return f"{minute // 60:02d}:{minute % 60:02d}"


def read_csv(name):
    with (DATA / f"{name}.csv").open(encoding="utf-8", newline="") as handle:
        yield from csv.DictReader(handle)


def load_data():
    """读取全部交换表并转成可计算结构。"""
    journeys = {}
    for row in read_csv("journeys"):
        journeys[row["journey"]] = {
            "journey": row["journey"], "day": row["day"], "flow": row["flow"],
            "pickup_from": int(row["pickup_from"]),
            "checkin_until": int(row["checkin_until"]),
            "opens": int(row["opens"]),
            "replacement_from": (int(row["replacement_from"])
                                 if row["replacement_from"] else None),
            "result": row["result"], "fixed": row["fixed"] == "1"}
    proofs = defaultdict(dict)
    for row in read_csv("time_proofs"):
        proofs[row["journey"]][row["stage"]] = {
            "source": row["source"],
            "lo": int(row["earliest"]), "hi": int(row["latest"])}
    pauses = {row["journey"]: (int(row["from"]), int(row["to"]))
              for row in read_csv("family_changes")}
    rules = defaultdict(dict)
    for row in read_csv("flow_rules"):
        rules[row["flow"]][row["stage"]] = {
            "depends": tuple(row["depends"].split("|")),
            "min": int(row["min_minutes"])}
    variants = {row["variant"]: {"variant": row["variant"],
                                 "pickup_delta": int(row["pickup_delta"]),
                                 "closing_delta": int(row["closing_delta"]),
                                 "scope": row["scope"]}
                for row in read_csv("joint_variants")}
    schedule = defaultdict(dict)
    for row in read_csv("workday_schedule"):
        schedule[row["day"]][row["journey"]] = {
            "vehicle": row["vehicle"], "escort": row["escort"],
            "turnaround": int(row["turnaround"])}
    acceptance = {row["variant"]: dict(row) for row in read_csv("window_acceptance")}
    return {"journeys": journeys, "proofs": proofs, "pauses": pauses,
            "rules": rules, "variants": variants, "schedule": schedule,
            "acceptance": acceptance}


# ---------------------------------------------------------------- 复盘引擎

def criticality(known, missing):
    """合取汇合的责任分析。

    known: {环节: (lo, hi)} 已核验就绪区间；missing: 无凭证环节列表。
    返回 (确定责任方列表, 可能责任方列表)。
    任一方凭证缺失时不存在确定责任——未知方可能才是最晚的。
    """
    definite, possible = [], []
    for stage, (lo, hi) in known.items():
        others_hi = max((h for s, (_, h) in known.items() if s != stage),
                        default=None)
        others_lo = max((l for s, (l, _) in known.items() if s != stage),
                        default=None)
        if not missing and others_hi is not None and lo >= others_hi:
            definite.append(PARTY_NAME[stage])
        if others_lo is None or hi >= others_lo:
            possible.append(PARTY_NAME[stage])
    if missing:
        possible.append(MISSING_PARTY)
    if len(definite) == 1:
        return definite, definite
    return [], possible


def reconstruct(journey, proofs, pause, flow_rules, variants):
    """由已核验区间重建单次行程，返回可 JSON 化的复盘记录。"""
    j = journey
    rv = {"journey": j["journey"], "day": j["day"], "flow": j["flow"],
          "result": j["result"], "result_name": OUTCOME_NAME[j["result"]],
          "fixed": j["fixed"],
          "anchors": {"pickup_from": j["pickup_from"],
                      "checkin_until": j["checkin_until"],
                      "opens": j["opens"],
                      "replacement_from": j["replacement_from"]},
          "evidence_gaps": [], "segments": [], "charges": [],
          "overlaps": [], "miss": None, "cancel": None,
          "replacement_wait": None, "single_change": []}
    pr = proofs
    P, U, O, R = (j["pickup_from"], j["checkin_until"],
                  j["opens"], j["replacement_from"])
    travel_min = flow_rules["arrive"]["min"]
    return_min = flow_rules["return"]["min"]

    def charge(segment, parties, lo, hi, kind, note=""):
        if hi <= 0:
            return
        rv["charges"].append({"segment": segment, "parties": list(parties),
                              "lo": lo, "hi": hi, "kind": kind, "note": note})

    missing_join = [s for s in JOIN_STAGES if s not in pr]
    if missing_join:
        rv["evidence_gaps"].append(
            "无已核验凭证: " + ",".join(missing_join))

    # ---- 接人汇合：depart 需 family|car|escort 全部就绪 ----
    dep = pr.get("depart")
    if dep:
        late = (max(0, dep["lo"] - P), max(0, dep["hi"] - P))
        seg = {"name": "接人汇合", "plan": P,
               "actual": [dep["lo"], dep["hi"]], "delay": list(late),
               "notes": []}
        bound = P
        if pause:
            bound = max(P, pause[1])
            seg["notes"].append(
                f"家属认可暂停 {fmt(pause[0])}–{fmt(pause[1])}")
        prov = (max(0, dep["lo"] - bound), max(0, dep["hi"] - bound))
        pause_min = (late[0] - prov[0], late[1] - prov[1])
        if pause_min[1] > 0:
            charge("接人汇合", [PAUSE_PARTY], pause_min[0], pause_min[1],
                   "pause", "主动暂停覆盖，不记服务方超时")
            rv["overlaps"].append(
                f"接人迟延 [{late[0]},{late[1]}] 与家属暂停 "
                f"{fmt(pause[0])}–{fmt(pause[1])} 重叠 "
                f"[{pause_min[0]},{pause_min[1]}] 分钟 → 重叠部分不记服务方")
        if prov[1] > 0:
            known = {s: (pr[s]["lo"], pr[s]["hi"])
                     for s in JOIN_STAGES if s in pr}
            ready_hi = max([h for _, h in known.values()] + [bound])
            if ready_hi <= bound and prov[0] > 0:
                charge("接人汇合", ["出发组织(未归属)"], prov[0], prov[1],
                       "operational", "各方就绪后仍未出发")
            else:
                definite, possible = criticality(known, missing_join)
                if definite:
                    charge("接人汇合", definite, prov[0], prov[1],
                           "determinate", "就绪确定最晚")
                else:
                    charge("接人汇合", possible, prov[0], prov[1],
                           "contested",
                           "就绪区间重叠，先后无法辨认；同一段分钟只计一次")
                    rv["overlaps"].append(
                        "接人汇合责任区间重叠: " + "/".join(possible)
                        + f" 均可能最晚 [{prov[0]},{prov[1]}] 分钟")
            if dep["lo"] > ready_hi:
                seg["notes"].append(
                    f"出发晚于各方就绪上限 [{dep['lo'] - ready_hi},"
                    f"{dep['hi'] - ready_hi}]，属出发组织")
        rv["segments"].append(seg)

    # ---- 在途 ----
    arr = pr.get("arrive")
    if dep and arr:
        extra = (max(0, arr["lo"] - dep["hi"] - travel_min),
                 max(0, arr["hi"] - dep["lo"] - travel_min))
        rv["segments"].append({
            "name": "在途", "plan": travel_min,
            "actual": [arr["lo"] - dep["hi"], arr["hi"] - dep["lo"]],
            "delay": list(extra),
            "notes": [f"最短业务耗时 {travel_min} 分钟"]})
        if extra[1] > 0:
            charge("在途", [PARTY_NAME["car"]], extra[0], extra[1],
                   "en_route", "超过最短耗时，含交通状况")

    # ---- 报到 / 错过 / 替补 / 取消 ----
    chk = pr.get("checkin")
    canc = pr.get("hospital_canceled")
    if j["result"] == "served" and chk and arr:
        base = (max(arr["lo"], O), max(arr["hi"], O))
        queue = (max(0, chk["lo"] - base[1]), max(0, chk["hi"] - base[0]))
        rv["segments"].append({
            "name": "院方报到等候", "plan": O,
            "actual": [chk["lo"], chk["hi"]], "delay": list(queue),
            "notes": []})
        if queue[1] > 0:
            charge("院方报到等候", [HOSPITAL], queue[0], queue[1],
                   "hospital", "报到晚于到院与开诊")
        if arr["hi"] < O:
            rv["overlaps"].append(
                f"到院 [{arr['lo']},{arr['hi']}] 早于开诊 {fmt(O)}，"
                "上游迟延被开诊等候吸收，未拖慢行程")
        if chk["lo"] > U:
            rv["evidence_gaps"].append(
                f"报到凭证 [{chk['lo']},{chk['hi']}] 晚于截止 {fmt(U)}，"
                "与台账结果 served 不一致")
    elif j["result"] == "replacement" and chk and arr:
        margin = (arr["lo"] - U, arr["hi"] - U)
        status = ("确定错过" if margin[0] > 0
                  else "可能错过" if margin[1] > 0 else "凭证内未错过")
        causes_det, causes_pos = upstream_causes(rv["charges"])
        rv["miss"] = {"margin": list(margin), "status": status,
                      "cause_determinate": causes_det,
                      "cause_possible": causes_pos}
        slot = (max(0, min(chk["lo"], R) - arr["hi"]),
                max(0, min(chk["hi"], R) - arr["lo"])) if R else (0, 0)
        queue = (max(0, chk["lo"] - max(arr["hi"], R or 0)),
                 max(0, chk["hi"] - max(arr["lo"], R or 0)))
        rv["replacement_wait"] = {"slot": list(slot), "queue": list(queue)}
        rv["segments"].append({
            "name": "替补等待", "plan": R,
            "actual": [chk["lo"], chk["hi"]],
            "delay": [slot[0] + queue[0], slot[1] + queue[1]],
            "notes": ["等替补窗属系统安排，不全摊入原窗口责任方"]})
        charge("替补等待", [SYSTEM_PARTY], slot[0], slot[1], "system",
               "等替补窗，不全摊入原窗口责任方")
        charge("院方报到等候", [HOSPITAL], queue[0], queue[1], "hospital",
               "替补窗后仍未报到")
        if slot[1] > 0:
            rv["overlaps"].append(
                f"等替补窗 [{slot[0]},{slot[1]}] 分钟与原窗口责任分离，"
                "不全摊入任何责任方")
    elif j["result"] == "no_replacement" and canc and arr:
        late = (arr["lo"] - U, arr["hi"] - U)
        if canc["hi"] < arr["lo"]:
            order = "取消先于到院"
        elif arr["hi"] < canc["lo"]:
            order = "到院先于取消"
        else:
            order = "先后无法辨认"
        causes_det, causes_pos = upstream_causes(rv["charges"])
        rv["cancel"] = {
            "cancel_at": [canc["lo"], canc["hi"]], "arrive": [arr["lo"], arr["hi"]],
            "late_margin": list(late), "order": order,
            "on_time": arr["hi"] <= U,
            "cause_determinate": causes_det, "cause_possible": causes_pos}
        rv["segments"].append({
            "name": "院方取消", "plan": U,
            "actual": [canc["lo"], canc["hi"]], "delay": [0, 0],
            "notes": [order + ("；到院在截止内，取消为院方决定"
                               if arr["hi"] <= U else "")]})
        if order == "先后无法辨认":
            rv["overlaps"].append(
                f"取消 [{canc['lo']},{canc['hi']}] 与到院 "
                f"[{arr['lo']},{arr['hi']}] 先后无法辨认 → "
                "保留不同归因成立的范围")

    # ---- 送回 ----
    cd, rc, ret = pr.get("care_done"), pr.get("return_car"), pr.get("return")
    if ret and (cd or rc):
        depart_home = (ret["lo"] - return_min, ret["hi"] - return_min)
        known2 = {}
        if cd:
            known2["院方护理完成"] = (cd["lo"], cd["hi"])
        if rc:
            known2["返程车到位"] = (rc["lo"], rc["hi"])
        ready_hi = max(h for _, h in known2.values())
        ready_lo = max(l for l, _ in known2.values())
        wait = (max(0, depart_home[0] - ready_hi),
                max(0, depart_home[1] - ready_lo))
        rv["segments"].append({
            "name": "送回等候", "plan": return_min,
            "actual": [ret["lo"], ret["hi"]], "delay": list(wait),
            "notes": []})
        if wait[1] > 0:
            definite = [p for p, (l, _) in known2.items()
                        if l >= max(h for q, (_, h) in known2.items() if q != p)]
            if len(definite) == 1:
                charge("送回等候", definite, wait[0], wait[1], "determinate",
                       "返程汇合确定最晚")
            else:
                possible = [p for p, (_, h) in known2.items() if h >= ready_lo]
                charge("送回等候", possible, wait[0], wait[1], "contested",
                       "返程汇合先后无法辨认")

    rv["single_change"] = single_change_tests(j, pr, pause, flow_rules, variants)
    return rv


def upstream_causes(charges):
    """接续链上游（接人汇合+在途）的归因，用于解释错过/取消的原因。

    确定归因要求分钟下限大于零——仅有区间上限的等待不算确定造成。
    """
    det, pos = set(), set()
    for c in charges:
        if c["segment"] not in ("接人汇合", "在途"):
            continue
        if c["lo"] > 0 and c["kind"] in ("determinate", "pause", "en_route",
                                         "operational"):
            det.update(c["parties"])
        pos.update(c["parties"])
    return sorted(det), sorted(pos)


# ---------------------------------------------------------------- 单改与试算

def project(journey, proofs, pause, travel_min, pickup_delta, closing_delta):
    """用同一批凭证在调整后窗口下重算到院与结果。

    接人提前量平移出发与到院；家属暂停不随窗口移动（保守口径：
    出发不早于暂停结束）。返回 (结论字典, 是否证据缺口)。
    """
    j = journey
    dep, arr = proofs.get("depart"), proofs.get("arrive")
    if not dep or not arr:
        return {"outcome": None, "note": "缺 depart/arrive 凭证"}, True
    shift = pickup_delta
    arr_new = [arr["lo"] + shift, arr["hi"] + shift]
    dep_new = [dep["lo"] + shift, dep["hi"] + shift]
    note = ""
    if pause and pause[1] > dep_new[0]:
        t_lo = max(travel_min, arr["lo"] - dep["hi"])
        t_hi = max(travel_min, arr["hi"] - dep["lo"])
        dep_new = [max(dep_new[0], pause[1]), max(dep_new[1], pause[1])]
        arr_new = [max(arr_new[0], pause[1] + t_lo),
                   max(arr_new[1], pause[1] + t_hi)]
        note = f"家属暂停至 {fmt(pause[1])}，接人提前受限"
    until = j["checkin_until"] + closing_delta
    if arr_new[1] <= until:
        outcome, certainty = "served", "确定"
    elif arr_new[0] <= until:
        outcome, certainty = "served", "可能"
    elif j["replacement_from"]:
        outcome, certainty = "replacement", "确定"
    else:
        outcome, certainty = "canceled", "确定"
    return {"outcome": outcome, "certainty": certainty,
            "depart": dep_new, "arrive": arr_new,
            "until": until, "note": note}, False


def single_change_tests(journey, proofs, pause, flow_rules, variants):
    """对错过原窗口的行程，逐项测试已登记方案的单一杠杆能否赶回原预约。"""
    if journey["result"] == "served":
        return []
    travel_min = flow_rules["arrive"]["min"]
    tests = []
    for v in variants.values():
        if v["scope"] != journey["flow"]:
            continue
        levers = [("仅接人提前", v["pickup_delta"], 0),
                  ("仅截止延后", 0, v["closing_delta"])]
        if v["pickup_delta"] and v["closing_delta"]:
            levers.append(("两改并用", v["pickup_delta"], v["closing_delta"]))
        for lever, pd, cd in levers:
            if pd == 0 and cd == 0:
                continue
            proj, gap = project(journey, proofs, pause, travel_min, pd, cd)
            if gap:
                tests.append({"variant": v["variant"], "lever": lever,
                              "outcome": "证据缺口"})
                continue
            ok = proj["outcome"] == "served"
            if journey["result"] == "no_replacement":
                outcome = ("到院可赶回·取消不变" if ok else "仍赶不上·取消不变")
                note = (proj["note"] + "；" if proj["note"] else "") + \
                    "取消属院方决定，不计承诺改善"
            else:
                outcome = ("可赶回" if ok and proj["certainty"] == "确定"
                           else "可能赶回" if ok else "赶不回原窗口")
                note = proj["note"]
            tests.append({
                "variant": v["variant"], "lever": lever,
                "pickup_delta": pd, "closing_delta": cd,
                "outcome": outcome,
                "arrive": proj["arrive"], "until": proj["until"],
                "note": note})
    return tests


# ---------------------------------------------------------------- 汇总报告

def build_reviews(data):
    reviews = {}
    for jid, j in data["journeys"].items():
        reviews[jid] = reconstruct(j, data["proofs"].get(jid, {}),
                                   data["pauses"].get(jid),
                                   data["rules"][j["flow"]],
                                   data["variants"])
    return reviews


def aggregate(reviews, journeys):
    """按同类流程与时段汇总等待分布、脱落与归因支撑。"""
    groups = defaultdict(list)
    for jid, rv in reviews.items():
        j = journeys[jid]
        groups[(j["flow"], j["pickup_from"])].append(rv)
    out = []
    for (flow, slot), rvs in sorted(groups.items(),
                                    key=lambda kv: (kv[0][0], kv[0][1])):
        n = len(rvs)
        by_result = defaultdict(list)
        for rv in rvs:
            by_result[rv["result"]].append(rv["journey"])
        recon = {"total": n,
                 "served": by_result["served"],
                 "replacement": by_result["replacement"],
                 "canceled": by_result["no_replacement"]}
        assert n == sum(len(v) for v in by_result.values())  # 取消不退出母体

        segments = defaultdict(lambda: {"journeys": 0, "lo": 0, "hi": 0})
        party = defaultdict(lambda: {"det_journeys": set(), "pos_journeys": set(),
                                     "det_min": 0, "pos_min": 0})
        combined_hi = 0
        miss_support = defaultdict(lambda: {"det": set(), "pos": set()})
        for rv in rvs:
            seen_seg = set()
            for c in rv["charges"]:
                key = c["segment"]
                if (key, tuple(c["parties"])) not in seen_seg:
                    segments[key]["journeys"] += 1
                    seen_seg.add((key, tuple(c["parties"])))
                segments[key]["lo"] += c["lo"]
                segments[key]["hi"] += c["hi"]
                if c["kind"] == "system":
                    continue  # 替补等待不摊入任何责任方
                if c["lo"] > 0 and c["kind"] in ("determinate", "pause",
                                                 "en_route", "operational",
                                                 "hospital"):
                    for p in c["parties"]:
                        party[p]["det_journeys"].add(rv["journey"])
                        party[p]["det_min"] += c["lo"]
                for p in c["parties"]:
                    party[p]["pos_journeys"].add(rv["journey"])
                    party[p]["pos_min"] += c["hi"]
                combined_hi += c["hi"]  # contested 每条只计一次
            for outcome_key, store in (("miss", "miss"), ("cancel", "cancel")):
                info = rv.get(outcome_key)
                if not info:
                    continue
                for p in info["cause_determinate"]:
                    miss_support[p]["det"].add(rv["journey"])
                for p in info["cause_possible"]:
                    miss_support[p]["pos"].add(rv["journey"])
        out.append({
            "flow": flow, "slot": slot, "slot_name": fmt(slot), "n": n,
            "outcomes": {k: sorted(v) for k, v in by_result.items()},
            "reconciliation": recon,
            "dropout": {
                "missed": len(by_result["replacement"]) + len(by_result["no_replacement"]),
                "canceled": len(by_result["no_replacement"]),
                "rate": round((len(by_result["replacement"]) + len(by_result["no_replacement"])) / n, 3)},
            "segments": {k: dict(v) for k, v in segments.items()},
            "attribution": {p: {"det_journeys": sorted(d["det_journeys"]),
                                "pos_journeys": sorted(d["pos_journeys"]),
                                "det_min": d["det_min"], "pos_min": d["pos_min"]}
                            for p, d in party.items()},
            "miss_support": {p: {"det": sorted(d["det"]), "pos": sorted(d["pos"])}
                             for p, d in miss_support.items()},
            "combined_minutes_hi": combined_hi,
            "evidence_gaps": sorted({g for rv in rvs for g in rv["evidence_gaps"]})})
    return out


# ---------------------------------------------------------------- 窗口试算

def simulate(data, reviews, scope, pickup_delta, closing_delta, label):
    """在指定流程范围内按方案重算每个行程，并做资源周转检查。"""
    journeys, proofs = data["journeys"], data["proofs"]
    pauses, rules = data["pauses"], data["rules"]
    result = {"label": label, "scope": scope,
              "pickup_delta": pickup_delta, "closing_delta": closing_delta,
              "guarded_fixed": [], "transitions": defaultdict(list),
              "still_missed": [], "cancel_notes": [], "evidence_gaps": [],
              "resource": [], "verdict": {}}
    timelines = {}
    for jid, j in sorted(journeys.items()):
        pr = proofs.get(jid, {})
        if j["flow"] != scope:
            continue
        if j["fixed"]:
            result["guarded_fixed"].append(jid)
            timelines[jid] = actual_timeline(pr)
            continue
        proj, gap = project(j, pr, pauses.get(jid),
                            rules[j["flow"]]["arrive"]["min"],
                            pickup_delta, closing_delta)
        if gap:
            result["evidence_gaps"].append(f"{jid}: {proj['note']}")
            timelines[jid] = actual_timeline(pr)
            continue
        before = j["result"]
        after = proj["outcome"]
        if before == "no_replacement":
            # 取消属院方决定：窗口改动不能承诺挽回，人次保持取消不变
            key = "院方取消→院方取消(不变)"
            canc = pr.get("hospital_canceled")
            if canc and proj["arrive"][1] <= canc["lo"]:
                result["cancel_notes"].append(
                    f"{jid}: 提前后或可在取消前到院，但取消属院方决定，不计承诺改善")
        else:
            key = f"{OUTCOME_NAME[before]}→{OUTCOME_NAME[after]}({proj['certainty']})"
        result["transitions"][key].append(jid)
        if before != "served" and after != "served" and before != "no_replacement":
            note = proj["note"] or f"到院 {proj['arrive']} 仍晚于截止 {proj['until']}"
            result["still_missed"].append({"journey": jid, "note": note})
        timelines[jid] = projected_timeline(j, pr, proj, pickup_delta,
                                            closing_delta, rules[j["flow"]])
    result["transitions"] = {k: sorted(v) for k, v in result["transitions"].items()}
    # 资源周转：仅排班表覆盖的日期可核验，其余日期为证据缺口
    for day in sorted({j["day"] for j in journeys.values() if j["flow"] == scope}):
        day_rows = data["schedule"].get(day)
        if not day_rows:
            result["evidence_gaps"].append(f"{day}: 无排班表，周转无法核验")
            continue
        base_tl = {jid: actual_timeline(proofs.get(jid, {})) for jid in day_rows}
        result["resource"].extend(
            resource_check(day, day_rows, base_tl, timelines, journeys))
    # 结论
    det = sum(len(v) for k, v in result["transitions"].items()
              if k.startswith("替补完成→原窗口完成(确定)"))
    poss = sum(len(v) for k, v in result["transitions"].items()
               if k.startswith("替补完成→原窗口完成(可能)"))
    blocked_days = sorted({f["day"] for f in result["resource"]
                           if f["status"] == "不足"})
    gap_days = sorted({g.split(":")[0] for g in result["evidence_gaps"]
                       if "无排班表" in g})
    result["verdict"] = {
        "determinate_improved": det, "possible_improved": poss,
        "still_missed": len(result["still_missed"]),
        "blocked_days": blocked_days, "gap_days": gap_days,
        "advice": ("有证据部分可承诺；周转不足日与证据缺口日留作试用"
                   if blocked_days or gap_days else "样本内可承诺")}
    return result


def actual_timeline(pr):
    dep, ret = pr.get("depart"), pr.get("return")
    return {"depart": [dep["lo"], dep["hi"]] if dep else None,
            "return": [ret["lo"], ret["hi"]] if ret else None}


def projected_timeline(j, pr, proj, pickup_delta, closing_delta, flow_rule):
    """试算后的出发/到家区间：下游按报到移动量平移，护理时长不变。"""
    tl = {"depart": proj["depart"], "return": None}
    chk, ret = pr.get("checkin"), pr.get("return")
    if chk and ret:
        if proj["outcome"] == "served":
            base = j["opens"]
            chk_new = [max(proj["arrive"][0], base), max(proj["arrive"][1], base)]
        else:
            base = j["replacement_from"] or j["opens"]
            chk_new = [max(proj["arrive"][0], base), max(proj["arrive"][1], base)]
        shift = (chk_new[0] - chk["lo"], chk_new[1] - chk["hi"])
        tl["return"] = [ret["lo"] + shift[0], ret["hi"] + shift[1]]
    elif ret:
        tl["return"] = [ret["lo"] + pickup_delta, ret["hi"] + pickup_delta]
    return tl


def resource_check(day, day_rows, base_tl, new_tl, journeys):
    """同一车辆/连班陪诊员相邻行程：后程出发须不早于前程到家加周转余量。"""
    findings = []
    chains = defaultdict(list)
    for jid, info in day_rows.items():
        chains[("车辆", info["vehicle"])].append(jid)
        chains[("陪诊员", info["escort"])].append(jid)
    for (kind, rid), jids in sorted(chains.items()):
        jids.sort(key=lambda x: journeys[x]["pickup_from"])
        for prev, nxt in zip(jids, jids[1:]):
            ta = day_rows[nxt]["turnaround"]
            b, n = base_tl.get(prev, {}), base_tl.get(nxt, {})
            p, q = new_tl.get(prev, {}), new_tl.get(nxt, {})
            if not (b.get("return") and n.get("depart")):
                findings.append({"day": day, "resource": f"{kind}{rid}",
                                 "pair": [prev, nxt], "status": "证据缺口",
                                 "note": "前程到家或后程出发无凭证"})
                continue
            base_gap = [n["depart"][0] - b["return"][1],
                        n["depart"][1] - b["return"][0]]
            if not (p.get("return") and q.get("depart")):
                new_gap = None
            else:
                new_gap = [q["depart"][0] - p["return"][1],
                           q["depart"][1] - p["return"][0]]
            def status(gap):
                if gap is None:
                    return "证据缺口"
                if gap[0] >= ta:
                    return "满足"
                return "临界" if gap[1] >= ta else "不足"
            findings.append({"day": day, "resource": f"{kind}{rid}",
                             "pair": [prev, nxt], "turnaround": ta,
                             "base_gap": base_gap, "new_gap": new_gap,
                             "base_status": status(base_gap),
                             "status": status(new_gap)})
    return findings


def combine_variants(data, vids):
    """组合方案：同范围同杠杆即占相同窗口，不可一起承诺。"""
    variants = data["variants"]
    by_scope = defaultdict(list)
    for vid in vids:
        by_scope[variants[vid]["scope"]].append(variants[vid])
    conflicts, merged = [], {}
    for scope, vs in by_scope.items():
        p_users = [v["variant"] for v in vs if v["pickup_delta"]]
        c_users = [v["variant"] for v in vs if v["closing_delta"]]
        if len(p_users) > 1:
            conflicts.append(f"{'+'.join(p_users)} 同占 {scope} 接人窗")
        if len(c_users) > 1:
            conflicts.append(f"{'+'.join(c_users)} 同占 {scope} 报到截止")
        merged[scope] = (sum(v["pickup_delta"] for v in vs),
                         sum(v["closing_delta"] for v in vs))
    notes = []
    for scope, (pd, cd) in merged.items():
        for v in variants.values():
            if (v["scope"] == scope and v["pickup_delta"] == pd
                    and v["closing_delta"] == cd and v["variant"] not in vids):
                notes.append(f"组合 {scope}({pd:+},{cd:+}) 等同于已登记方案 "
                             f"{v['variant']}")
    if len(by_scope) > 1:
        notes.append("方案分属不同流程日期，同一车辆/陪诊员不跨日占用，无资源冲突")
    return merged, conflicts, notes


# ---------------------------------------------------------------- 核准复核

def acceptance_review(data, reviews):
    """已核准方案自生效日起按真实窗口重判；未生效方案留作试用。"""
    journeys, variants = data["journeys"], data["variants"]
    out = []
    for vid, row in sorted(data["acceptance"].items()):
        v = variants[vid]
        in_force = row["hospital"] == "confirmed" and row["dispatch"] == "confirmed"
        eff = sorted(jid for jid, j in journeys.items()
                     if row["starts"] <= j["day"] <= row["ends"]
                     and j["flow"] == v["scope"])
        entry = {"variant": vid, "window": [row["starts"], row["ends"]],
                 "hospital": row["hospital"], "dispatch": row["dispatch"],
                 "in_force": in_force, "effective_journeys": eff}
        if not in_force:
            entry["status"] = "未生效（需双方确认），试算结论留作试用"
            out.append(entry)
            continue
        reclass = {"served_det": [], "served_poss": [], "still_missed": [],
                   "canceled": []}
        for jid in eff:
            j = journeys[jid]
            if j["result"] == "served":
                continue
            if j["result"] == "no_replacement":
                reclass["canceled"].append(jid)
                continue
            proj, gap = project(j, data["proofs"].get(jid, {}),
                                data["pauses"].get(jid),
                                data["rules"][j["flow"]]["arrive"]["min"],
                                v["pickup_delta"], v["closing_delta"])
            if gap:
                continue
            if proj["outcome"] == "served" and proj["certainty"] == "确定":
                reclass["served_det"].append(jid)
            elif proj["outcome"] == "served":
                reclass["served_poss"].append(jid)
            else:
                reclass["still_missed"].append(jid)
        baseline = sorted(jid for jid, j in journeys.items()
                          if j["flow"] == v["scope"]
                          and not row["starts"] <= j["day"] <= row["ends"])
        base_missed = [jid for jid in baseline
                       if journeys[jid]["result"] != "served"]
        eff_missed = [jid for jid in eff if journeys[jid]["result"] != "served"]
        entry["reclassified"] = reclass
        entry["baseline"] = {"journeys": len(baseline), "missed": len(base_missed)}
        entry["effective_actual"] = {"journeys": len(eff), "missed": len(eff_missed)}
        entry["effective_under_window"] = {
            "missed_determinate": len(reclass["still_missed"]) + len(reclass["canceled"]),
            "missed_possible": len(reclass["served_poss"])}
        entry["verdict"] = (
            f"按真实窗口重判：确定改善 {len(reclass['served_det'])} 人次"
            f"（{','.join(reclass['served_det']) or '无'}），"
            f"可能改善 {len(reclass['served_poss'])} 人次")
        out.append(entry)
    return out


# ---------------------------------------------------------------- 文本呈现

def render_journey(rv):
    a = rv["anchors"]
    lines = [f"行程 {rv['journey']} · {rv['day']} · {rv['flow']} 流程 · "
             f"结果: {rv['result_name']}" + (" · 锁定预约" if rv["fixed"] else "")]
    plan = f"计划: 接人 {fmt(a['pickup_from'])} · 开诊 {fmt(a['opens'])} · 截止 {fmt(a['checkin_until'])}"
    if a["replacement_from"]:
        plan += f" · 替补窗 {fmt(a['replacement_from'])}"
    lines.append(plan)
    for g in rv["evidence_gaps"]:
        lines.append(f"  证据缺口: {g}")
    for seg in rv["segments"]:
        act = (f"[{seg['actual'][0]},{seg['actual'][1]}]"
               if seg["actual"] else "无凭证")
        lines.append(f"  {seg['name']:<6} 计划/基准 {seg['plan']} · 实际 {act} "
                     f"· 延误 [{seg['delay'][0]},{seg['delay'][1]}]")
        for c in rv["charges"]:
            if c["segment"] != seg["name"]:
                continue
            kind = {"determinate": "确定", "contested": "可能·只计一次",
                    "pause": "家属暂停", "system": "系统·不全摊",
                    "hospital": "院方", "en_route": "在途",
                    "operational": "未归属"}[c["kind"]]
            lines.append(f"    归因: {'/'.join(c['parties'])} "
                         f"[{c['lo']},{c['hi']}]（{kind}）"
                         + (f" {c['note']}" if c["note"] else ""))
        for note in seg["notes"]:
            lines.append(f"    · {note}")
    if rv["miss"]:
        m = rv["miss"]
        lines.append(f"  错过原窗口: {m['status']}，幅度 "
                     f"[{max(0, m['margin'][0])},{max(0, m['margin'][1])}] 分钟")
        lines.append(f"    归因 确定: {'/'.join(m['cause_determinate']) or '无'} · "
                     f"可能: {'/'.join(m['cause_possible']) or '无'}")
    if rv["cancel"]:
        c = rv["cancel"]
        lines.append(f"  院方取消: 取消 {c['cancel_at']} · 到院 {c['arrive']} · {c['order']}")
        lines.append(f"    到院迟延幅度 [{max(0, c['late_margin'][0])},"
                     f"{max(0, c['late_margin'][1])}] · "
                     f"归因 确定: {'/'.join(c['cause_determinate']) or '无'} · "
                     f"可能: {'/'.join(c['cause_possible']) or '无'}")
    if rv["replacement_wait"]:
        w = rv["replacement_wait"]
        lines.append(f"  替补等待: 等替补窗 [{w['slot'][0]},{w['slot'][1]}]（不全摊）"
                     f" · 窗后等候 [{w['queue'][0]},{w['queue'][1]}]")
    if rv["overlaps"]:
        lines.append("  重叠说明:")
        lines.extend(f"    · {o}" for o in rv["overlaps"])
    if rv["single_change"]:
        lines.append("  单改测试（已登记方案）:"
                     + ("锁定预约，仅供诊断，试算不移动" if rv["fixed"] else ""))
        for t in rv["single_change"]:
            extra = (f" 到院{t['arrive']} vs 截止{t['until']}"
                     if "arrive" in t else "")
            note = f"（{t['note']}）" if t.get("note") else ""
            lines.append(f"    · {t['variant']} {t['lever']}: {t['outcome']}"
                         f"{extra}{note}")
    return "\n".join(lines)


def render_report(groups):
    lines = []
    for g in groups:
        r = g["reconciliation"]
        lines.append(f"== {g['flow']} · 接人 {g['slot_name']} · {g['n']} 人次 ==")
        lines.append(f"  人次核对: 原窗口完成 {len(r['served'])} · 替补 "
                     f"{len(r['replacement'])} · 取消 {len(r['canceled'])}"
                     f"（合计 {r['total']}，取消保留在母体）")
        lines.append(f"    原窗口: {','.join(r['served']) or '无'}")
        lines.append(f"    替补:   {','.join(r['replacement']) or '无'}")
        lines.append(f"    取消:   {','.join(r['canceled']) or '无'}")
        d = g["dropout"]
        lines.append(f"  脱落: 错过原窗口 {d['missed']}/{g['n']}"
                     f"（{d['rate']:.1%}）· 其中院方取消 {d['canceled']}")
        lines.append("  等待分布（分钟，确定下限/含可能上限）:")
        for name, s in g["segments"].items():
            lines.append(f"    {name:<8} 涉及 {s['journeys']} 人次 · "
                         f"[{s['lo']},{s['hi']}]")
        lines.append("  归因支撑（确定 / 含可能）:")
        for p, a in sorted(g["attribution"].items()):
            ms = g["miss_support"].get(p, {"det": [], "pos": []})
            lines.append(f"    {p}: {len(a['det_journeys'])}/{len(a['pos_journeys'])} 人次 · "
                         f"{a['det_min']}/{a['pos_min']} 分钟 · "
                         f"导致错过 确定{len(ms['det'])} 可能{len(ms['pos'])}")
        lines.append(f"  累计影响（可合计口径，竞争分钟只计一次）: "
                     f"{g['combined_minutes_hi']} 分钟")
        for e in g["evidence_gaps"]:
            lines.append(f"  证据缺口: {e}")
        lines.append("")
    return "\n".join(lines)


def render_whatif(results):
    lines = []
    for res in results:
        lines.append(f"== 试算 {res['label']} · {res['scope']} · "
                     f"接人 {res['pickup_delta']:+} · 截止 {res['closing_delta']:+} ==")
        if res["guarded_fixed"]:
            lines.append(f"  锁定预约不动: {','.join(res['guarded_fixed'])}")
        for k, v in sorted(res["transitions"].items()):
            lines.append(f"  {k}: {len(v)} 人次"
                         + (f"（{','.join(v)}）" if len(v) <= 12 else ""))
        for s in res["still_missed"]:
            lines.append(f"  仍错过: {s['journey']}（{s['note']}）")
        for n in res["cancel_notes"]:
            lines.append(f"  取消说明: {n}")
        if res["resource"]:
            lines.append("  周转核验（后程出发 − 前程到家 vs 余量）:")
            for f in res["resource"]:
                if f["status"] == "证据缺口":
                    lines.append(f"    {f['day']} {f['resource']} "
                                 f"{'→'.join(f['pair'])}: 证据缺口（{f['note']}）")
                    continue
                mark = {"满足": "✓", "临界": "△", "不足": "✗"}[f["status"]]
                base = f"基线{f['base_gap']}({f['base_status']})"
                lines.append(f"    {f['day']} {f['resource']} "
                             f"{'→'.join(f['pair'])}: 试算{f['new_gap']} "
                             f"余量{f['turnaround']} {mark} · {base}")
        for g in res["evidence_gaps"]:
            lines.append(f"  证据缺口: {g}")
        v = res["verdict"]
        lines.append(f"  结论: 确定改善 {v['determinate_improved']} 人次 · "
                     f"可能 {v['possible_improved']} · 仍错过 {v['still_missed']} · "
                     f"{v['advice']}")
        lines.append("")
    return "\n".join(lines)


def render_acceptance(entries):
    lines = []
    for e in entries:
        head = f"== 核准 {e['variant']} · 窗口 {e['window'][0]}…{e['window'][1]} =="
        lines.append(head)
        lines.append(f"  院方 {e['hospital']} · 接送 {e['dispatch']}")
        if not e["in_force"]:
            lines.append(f"  {e['status']}")
            continue
        lines.append(f"  覆盖行程 {len(e['effective_journeys'])} 人次: "
                     f"{','.join(e['effective_journeys']) or '无'}")
        b, a = e["baseline"], e["effective_actual"]
        lines.append(f"  对照: 基线错过 {b['missed']}/{b['journeys']} · "
                     f"生效期实际错过 {a['missed']}/{a['journeys']}")
        r = e["reclassified"]
        lines.append(f"  按真实窗口重判: 确定可回原窗口 {len(r['served_det'])}"
                     f"（{','.join(r['served_det']) or '无'}）· "
                     f"可能 {len(r['served_poss'])}"
                     f"（{','.join(r['served_poss']) or '无'}）· "
                     f"仍错过 {len(r['still_missed'])} · 取消不变 {len(r['canceled'])}")
        lines.append(f"  {e['verdict']}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def main():
    parser = ArgumentParser(description="行程复盘与窗口试算")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    sub = parser.add_subparsers(dest="cmd")

    p_j = sub.add_parser("journey", help="单次行程复盘")
    p_j.add_argument("ids", nargs="+", help="行程号，如 E005")

    p_r = sub.add_parser("report", help="按流程与时段汇总")
    p_r.add_argument("--flow", choices=["weekday", "weekend"])

    p_w = sub.add_parser("whatif", help="窗口试算")
    p_w.add_argument("variants", nargs="*",
                     help="方案号，缺省为全部已登记方案")
    p_w.add_argument("--combine", action="store_true",
                     help="组合所列方案（检查是否可一起承诺）")

    sub.add_parser("acceptance", help="核准复核")
    options = parser.parse_args()

    data = load_data()
    reviews = build_reviews(data)

    if options.cmd == "journey":
        unknown = [i for i in options.ids if i not in reviews]
        if unknown:
            parser.error(f"未知行程: {','.join(unknown)}")
        rvs = [reviews[i] for i in options.ids]
        if options.json:
            print(json.dumps(rvs, ensure_ascii=False, indent=2))
        else:
            print("\n\n".join(render_journey(rv) for rv in rvs))
    elif options.cmd == "report":
        groups = aggregate(reviews, data["journeys"])
        if options.flow:
            groups = [g for g in groups if g["flow"] == options.flow]
        if options.json:
            print(json.dumps(groups, ensure_ascii=False, indent=2))
        else:
            print(render_report(groups))
    elif options.cmd == "whatif":
        vids = options.variants or sorted(data["variants"])
        unknown = [v for v in vids if v not in data["variants"]]
        if unknown:
            parser.error(f"未知方案: {','.join(unknown)}")
        results = []
        if options.combine:
            merged, conflicts, notes = combine_variants(data, vids)
            if conflicts:
                msg = "不可一起承诺: " + "；".join(conflicts)
                if options.json:
                    print(json.dumps({"conflicts": conflicts, "notes": notes},
                                     ensure_ascii=False, indent=2))
                else:
                    print(msg)
                    for n in notes:
                        print(f"  · {n}")
                return
            for scope, (pd, cd) in sorted(merged.items()):
                label = "+".join(vids)
                res = simulate(data, reviews, scope, pd, cd, label)
                res["combine_notes"] = notes
                results.append(res)
        else:
            for vid in vids:
                v = data["variants"][vid]
                results.append(simulate(data, reviews, v["scope"],
                                        v["pickup_delta"], v["closing_delta"],
                                        vid))
        if options.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            print(render_whatif(results))
    elif options.cmd == "acceptance":
        entries = acceptance_review(data, reviews)
        if options.json:
            print(json.dumps(entries, ensure_ascii=False, indent=2))
        else:
            print(render_acceptance(entries))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
