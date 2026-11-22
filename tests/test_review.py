"""复盘与窗口试算的口径测试：锚定关键业务规则，防止回归。"""
import unittest

from review import (aggregate, build_reviews, combine_variants, load_data,
                    acceptance_review, simulate)

DATA = load_data()
REVIEWS = build_reviews(DATA)


def charges(jid, segment=None):
    items = REVIEWS[jid]["charges"]
    if segment:
        items = [c for c in items if c["segment"] == segment]
    return items


class ReconstructTest(unittest.TestCase):
    def test_join_contested_when_readiness_intervals_overlap(self):
        # E001 三方就绪区间互相重叠：无确定责任，同一段分钟只计一次
        [c] = charges("E001", "接人汇合")
        self.assertEqual(c["kind"], "contested")
        self.assertEqual(set(c["parties"]), {"家属", "接送队", "陪诊组"})
        self.assertEqual((c["lo"], c["hi"]), (12, 27))

    def test_join_determinate_when_one_party_strictly_latest(self):
        # E009 接送就绪 [578,587] 确定晚于其余各方
        [c] = charges("E009", "接人汇合")
        self.assertEqual(c["kind"], "determinate")
        self.assertEqual(c["parties"], ["接送队"])
        self.assertEqual((c["lo"], c["hi"]), (68, 72))

    def test_missing_proofs_keep_attribution_open(self):
        # E033 无 family/escort 凭证：不能定责，须保留凭证缺失方
        [c] = charges("E033", "接人汇合")
        self.assertEqual(c["kind"], "contested")
        self.assertIn("凭证缺失方", c["parties"])
        self.assertIn("无已核验凭证", REVIEWS["E033"]["evidence_gaps"][0])

    def test_family_pause_not_counted_as_provider_overtime(self):
        # E005 暂停 520-564 覆盖全部接人迟延：服务方分钟为零
        pause = [c for c in charges("E005", "接人汇合") if c["kind"] == "pause"]
        self.assertEqual(len(pause), 1)
        self.assertEqual((pause[0]["lo"], pause[0]["hi"]), (40, 44))
        self.assertEqual(REVIEWS["E005"]["miss"]["cause_determinate"], ["家属暂停"])

    def test_pause_before_window_has_no_effect(self):
        # E031 暂停 548-576 早于接人窗 610：不产生暂停分钟
        self.assertFalse([c for c in charges("E031") if c["kind"] == "pause"])

    def test_cross_midnight_pause_and_fixed(self):
        # E064 跨午夜锁定行程：暂停覆盖接人迟延，不错乱
        rv = REVIEWS["E064"]
        self.assertTrue(rv["fixed"])
        [c] = [c for c in charges("E064", "接人汇合") if c["kind"] == "pause"]
        self.assertEqual((c["lo"], c["hi"]), (46, 50))
        self.assertEqual(rv["miss"]["status"], "可能错过")
        self.assertEqual(rv["miss"]["margin"], [-2, 2])

    def test_replacement_wait_split_not_dumped_on_original_party(self):
        # E005 等替补窗 [19,23] 属系统安排；窗后等候 [0,10] 才归院方
        w = REVIEWS["E005"]["replacement_wait"]
        self.assertEqual(w["slot"], [19, 23])
        self.assertEqual(w["queue"], [0, 10])
        [sys_c] = [c for c in charges("E005") if c["kind"] == "system"]
        self.assertEqual(sys_c["parties"], ["替补系统"])

    def test_cancel_order_classification(self):
        self.assertEqual(REVIEWS["E061"]["cancel"]["order"], "取消先于到院")
        self.assertEqual(REVIEWS["E006"]["cancel"]["order"], "到院先于取消")
        # E016 取消与到院区间完全重叠：保留不同归因成立的范围
        rv = REVIEWS["E016"]
        self.assertEqual(rv["cancel"]["order"], "先后无法辨认")
        self.assertEqual(rv["cancel"]["cause_determinate"], [])
        self.assertEqual(set(rv["cancel"]["cause_possible"]),
                         {"家属", "接送队", "陪诊组"})

    def test_single_change_tests(self):
        # E009 差 35-39 分钟：两项单改都赶不回，两改并用才可
        tests = {(t["variant"], t["lever"]): t["outcome"]
                 for t in REVIEWS["E009"]["single_change"]}
        self.assertEqual(tests[("J-A", "仅接人提前")], "赶不回原窗口")
        self.assertEqual(tests[("J-B", "仅截止延后")], "赶不回原窗口")
        self.assertEqual(tests[("J-C", "两改并用")], "可赶回")
        # E005 家属暂停约束下仅提前接人无效，仅延后截止可赶回
        tests = {(t["variant"], t["lever"]): t["outcome"]
                 for t in REVIEWS["E005"]["single_change"]}
        self.assertEqual(tests[("J-A", "仅接人提前")], "赶不回原窗口")
        self.assertEqual(tests[("J-B", "仅截止延后")], "可赶回")
        # E016 院方取消：即使到院可赶回也不计承诺
        outcomes = {t["outcome"] for t in REVIEWS["E016"]["single_change"]}
        self.assertEqual(outcomes, {"到院可赶回·取消不变"})


class AggregateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.groups = aggregate(REVIEWS, DATA["journeys"])

    def test_reconciliation_counts_and_population(self):
        # 原窗口+替补+取消=母体；取消不消失；替补不重复计为完成
        total = 0
        for g in self.groups:
            r = g["reconciliation"]
            self.assertEqual(r["total"], len(r["served"]) + len(r["replacement"])
                             + len(r["canceled"]))
            self.assertFalse(set(r["served"]) & set(r["replacement"]))
            total += r["total"]
        self.assertEqual(total, len(DATA["journeys"]))
        canceled = sum(len(g["reconciliation"]["canceled"]) for g in self.groups)
        self.assertEqual(canceled, 9)

    def test_contested_minutes_counted_once_in_combined(self):
        # E001 接人汇合的竞争分钟只在合并口径出现一次
        g = next(g for g in self.groups
                 if g["flow"] == "weekday" and g["slot"] == 510)
        e001 = [c for c in REVIEWS["E001"]["charges"]
                if c["kind"] == "contested" and c["segment"] == "接人汇合"]
        self.assertEqual(len(e001), 1)
        per_party_sum = sum(a["pos_min"] for a in g["attribution"].values())
        self.assertGreater(per_party_sum, g["combined_minutes_hi"])


class WhatifTest(unittest.TestCase):
    def simulate(self, vid):
        v = DATA["variants"][vid]
        return simulate(DATA, REVIEWS, v["scope"], v["pickup_delta"],
                        v["closing_delta"], vid)

    def test_single_variant_outcomes(self):
        res = self.simulate("J-A")
        moved = res["transitions"].get("替补完成→原窗口完成(确定)", [])
        self.assertIn("E007", moved)
        self.assertNotIn("E009", moved)
        self.assertEqual({s["journey"] for s in res["still_missed"]},
                         {"E005", "E009", "E053"})
        self.assertEqual(res["transitions"].get("替补完成→原窗口完成(可能)"),
                         ["E025"])
        # 锁定预约不动
        self.assertEqual(set(res["guarded_fixed"]),
                         {"E001", "E010", "E028", "E037", "E055", "E064"})
        # 取消人次保持不变，不悄悄消失
        canceled = res["transitions"].get("院方取消→院方取消(不变)", [])
        self.assertEqual(len(canceled), 7)

    def test_combined_variant_recovers_joint_cases(self):
        res = self.simulate("J-C")
        moved = res["transitions"].get("替补完成→原窗口完成(确定)", [])
        self.assertTrue({"E005", "E009", "E053", "E025"} <= set(moved))

    def test_weekend_variant_insufficient_for_some(self):
        res = self.simulate("J-D")
        self.assertEqual({s["journey"] for s in res["still_missed"]},
                         {"E017", "E045"})
        res = self.simulate("J-E")
        self.assertEqual(res["still_missed"], [])

    def test_combine_rules(self):
        merged, conflicts, notes = combine_variants(DATA, ["J-A", "J-B"])
        self.assertFalse(conflicts)
        self.assertEqual(merged["weekday"], (-25, 25))
        self.assertTrue(any("J-C" in n for n in notes))
        _, conflicts, _ = combine_variants(DATA, ["J-A", "J-C"])
        self.assertTrue(any("接人窗" in c for c in conflicts))
        _, conflicts, _ = combine_variants(DATA, ["J-D", "J-E"])
        self.assertEqual(len(conflicts), 2)

    def test_resource_constraints_and_gaps(self):
        res = self.simulate("J-A")
        by_pair = {(f["day"], f["resource"], tuple(f["pair"])): f
                   for f in res["resource"]}
        f = by_pair[("2026-09-01", "车辆C1", ("E001", "E002"))]
        self.assertEqual(f["status"], "不足")
        self.assertEqual(f["base_status"], "不足")  # 基线已不足，如实呈现
        gap_days = [g for g in res["evidence_gaps"] if "无排班表" in g]
        self.assertTrue(any("2026-09-03" in g for g in gap_days))
        self.assertEqual(res["verdict"]["determinate_improved"], 19)


class AcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entries = {e["variant"]: e
                       for e in acceptance_review(DATA, REVIEWS)}

    def test_in_force_requires_both_confirmed(self):
        self.assertTrue(self.entries["J-B"]["in_force"])
        # J-D 接送方待确认：未生效，留作试用
        self.assertFalse(self.entries["J-D"]["in_force"])
        self.assertIn("留作试用", self.entries["J-D"]["status"])

    def test_rejudge_with_real_windows(self):
        e = self.entries["J-B"]
        self.assertEqual(len(e["effective_journeys"]), 12)
        self.assertEqual(set(e["reclassified"]["served_det"]),
                         {"E027", "E033", "E034", "E035"})
        self.assertEqual(e["reclassified"]["served_poss"], ["E025"])
        self.assertEqual(set(e["reclassified"]["canceled"]), {"E026", "E036"})


if __name__ == "__main__":
    unittest.main()
