"""基线对比引擎（app.compare）单元测试。"""
from app import compare


def make_issue(direction="backward", path="#", code="required_added",
               severity="error", message="m", reason="r"):
    return {
        "direction": direction, "path": path, "code": code,
        "severity": severity, "message": message, "reason": reason,
        "counterexample": None, "counterexample_path": None,
    }


def make_report(issues, backward_compatible=True, forward_compatible=True):
    def summarize(direction):
        subset = [i for i in issues if i["direction"] == direction]
        return {
            "errors": sum(1 for i in subset if i["severity"] == "error"),
            "warnings": sum(1 for i in subset if i["severity"] == "warning"),
            "total": len(subset),
        }

    return {
        "backward_compatible": backward_compatible,
        "forward_compatible": forward_compatible,
        "summary": {
            "backward": summarize("backward"),
            "forward": summarize("forward"),
            "total": len(issues),
            "errors": sum(1 for i in issues if i["severity"] == "error"),
            "warnings": sum(1 for i in issues if i["severity"] == "warning"),
        },
        "issues": issues,
    }


class TestNormalizeReason:
    def test_collapses_whitespace(self):
        assert compare.normalize_reason("  枚举值 \n 已移除\t") == "枚举值 已移除"

    def test_none_becomes_empty(self):
        assert compare.normalize_reason(None) == ""

    def test_strips_heuristic_suffix(self):
        from app.compat import HEURISTIC_SUFFIX
        base = "旧版未限制为整数，小数值在新版会被拒绝"
        assert compare.normalize_reason(base + HEURISTIC_SUFFIX) == compare.normalize_reason(base)


class TestIssueFingerprint:
    def test_stable_for_same_issue(self):
        issue = make_issue()
        assert compare.issue_fingerprint(issue) == compare.issue_fingerprint(dict(issue))

    def test_ignores_whitespace_jitter_in_reason(self):
        a = make_issue(reason="旧版允许类型 integer")
        b = make_issue(reason="  旧版允许类型\ninteger  ")
        assert compare.issue_fingerprint(a) == compare.issue_fingerprint(b)

    def test_ignores_heuristic_suffix(self):
        from app.compat import HEURISTIC_SUFFIX
        a = make_issue(reason="旧版未限制为整数，小数值在新版会被拒绝")
        b = make_issue(reason="旧版未限制为整数，小数值在新版会被拒绝" + HEURISTIC_SUFFIX)
        assert compare.issue_fingerprint(a) == compare.issue_fingerprint(b)

    def test_ignores_severity(self):
        a = make_issue(severity="error")
        b = make_issue(severity="warning")
        assert compare.issue_fingerprint(a) == compare.issue_fingerprint(b)

    def test_sensitive_to_direction_path_code_reason(self):
        base = make_issue()
        base_fp = compare.issue_fingerprint(base)
        assert compare.issue_fingerprint(make_issue(direction="forward")) != base_fp
        assert compare.issue_fingerprint(make_issue(path="#/properties/a")) != base_fp
        assert compare.issue_fingerprint(make_issue(code="type_removed")) != base_fp
        assert compare.issue_fingerprint(make_issue(reason="其他原因")) != base_fp

    def test_hex_prefix_format(self):
        fp = compare.issue_fingerprint(make_issue())
        assert len(fp) == 16
        int(fp, 16)  # 合法十六进制


class TestCompareReports:
    def test_classifies_all_four_categories(self):
        shared = make_issue(path="#/properties/a", code="min_length_increased",
                            reason="minLength 从 0 提高到 2")
        fixed_issue = make_issue(path="#", code="required_added",
                                 reason="旧版中 'b' 可以缺省")
        added_issue = make_issue(path="#", code="required_added",
                                 reason="旧版中 'c' 可以缺省")
        sev_base = make_issue(path="#", code="integer_required", severity="error",
                              reason="旧版未限制为整数")
        sev_cur = make_issue(path="#", code="integer_required", severity="warning",
                             reason="旧版未限制为整数")

        baseline = make_report([shared, fixed_issue, sev_base],
                               backward_compatible=False)
        current = make_report([shared, added_issue, sev_cur],
                              backward_compatible=False)

        result = compare.compare_reports(baseline, current)

        assert result["counts"] == {"added": 1, "fixed": 1, "persisting": 1,
                                    "severity_changed": 1}
        assert result["issues"]["added"][0]["reason"] == "旧版中 'c' 可以缺省"
        assert result["issues"]["fixed"][0]["reason"] == "旧版中 'b' 可以缺省"
        assert result["issues"]["persisting"][0]["code"] == "min_length_increased"
        changed = result["issues"]["severity_changed"][0]
        assert changed["baseline_severity"] == "error"
        assert changed["current_severity"] == "warning"
        assert changed["fingerprint"] == compare.issue_fingerprint(sev_base)

    def test_multiset_matching_by_occurrence(self):
        issue = make_issue()
        baseline = make_report([dict(issue), dict(issue)])  # 同指纹出现两次
        current = make_report([dict(issue)])
        result = compare.compare_reports(baseline, current)
        assert result["counts"]["persisting"] == 1
        assert result["counts"]["fixed"] == 1

    def test_interleaved_severities_pair_as_persisting(self):
        # 同一指纹：基线 error, warning；当前 warning, error（顺序交错）。
        # 严重级别多重集相同，必须全部配为 persisting，不能按出现顺序
        # 误报两条 severity_changed（即使错误 / 警告总数均未变化）。
        same = dict(path="#/properties/x", code="required_added", reason="r")
        baseline = make_report([
            make_issue(severity="error", **same),
            make_issue(severity="warning", **same),
        ])
        current = make_report([
            make_issue(severity="warning", **same),
            make_issue(severity="error", **same),
        ])

        result = compare.compare_reports(baseline, current)

        assert result["counts"] == {"added": 0, "fixed": 0, "persisting": 2,
                                    "severity_changed": 0}
        assert result["issues"]["severity_changed"] == []
        assert {i["severity"] for i in result["issues"]["persisting"]} == {
            "error", "warning"}
        # 总数不变 -> errors / warnings / total 的 delta 均为 0
        assert result["summary_diff"]["errors"]["delta"] == 0
        assert result["summary_diff"]["warnings"]["delta"] == 0
        assert result["summary_diff"]["total"]["delta"] == 0

    def test_interleaved_severities_with_real_change_and_extra(self):
        # 同一指纹的交错多重集，且夹带真实变化与数量差：
        # 基线 error×2, warning；当前 error, warning×2（先配同级别：
        # 1 error + 1 warning persisting，剩余 1 error↔1 warning 变化），
        # 当前还多出 1 warning -> added。
        same = dict(path="#/properties/y", code="type_removed", reason="r")
        baseline = make_report([
            make_issue(severity="error", **same),
            make_issue(severity="error", **same),
            make_issue(severity="warning", **same),
        ])
        current = make_report([
            make_issue(severity="warning", **same),
            make_issue(severity="error", **same),
            make_issue(severity="warning", **same),
            make_issue(severity="warning", **same),
        ])

        result = compare.compare_reports(baseline, current)

        assert result["counts"] == {"added": 1, "fixed": 0, "persisting": 2,
                                    "severity_changed": 1}
        changed = result["issues"]["severity_changed"]
        assert len(changed) == 1
        assert changed[0]["baseline_severity"] == "error"
        assert changed[0]["current_severity"] == "warning"
        assert result["issues"]["added"][0]["severity"] == "warning"
        assert result["summary_diff"]["errors"]["delta"] == -1
        assert result["summary_diff"]["warnings"]["delta"] == 2

    def test_interleaved_severities_extra_baseline_is_fixed(self):
        # 基线 error, warning；当前仅 warning：同级别 warning persisting，
        # 基线多出的 error 归 fixed（不应算作 severity_changed）。
        same = dict(path="#/properties/z", reason="r")
        baseline = make_report([
            make_issue(severity="error", **same),
            make_issue(severity="warning", **same),
        ])
        current = make_report([make_issue(severity="warning", **same)])

        result = compare.compare_reports(baseline, current)

        assert result["counts"] == {"added": 0, "fixed": 1, "persisting": 1,
                                    "severity_changed": 0}
        assert result["issues"]["fixed"][0]["severity"] == "error"

    def test_identical_reports_all_persisting(self):
        report = make_report([make_issue(), make_issue(code="enum_value_removed")])
        result = compare.compare_reports(report, report)
        assert result["counts"]["persisting"] == 2
        assert result["counts"]["added"] == 0
        assert result["counts"]["fixed"] == 0
        assert result["counts"]["severity_changed"] == 0

    def test_compatibility_and_summary_diff(self):
        baseline = make_report(
            [make_issue(severity="error"), make_issue(code="x", severity="warning")],
            backward_compatible=False, forward_compatible=True)
        current = make_report([], backward_compatible=True, forward_compatible=True)
        result = compare.compare_reports(baseline, current)

        compat = result["compatibility"]
        assert compat["backward"] == {"baseline": False, "current": True, "changed": True}
        assert compat["forward"] == {"baseline": True, "current": True, "changed": False}

        diff = result["summary_diff"]
        assert diff["errors"] == {"baseline": 1, "current": 0, "delta": -1}
        assert diff["warnings"] == {"baseline": 1, "current": 0, "delta": -1}
        assert diff["total"] == {"baseline": 2, "current": 0, "delta": -2}
        assert diff["backward"]["errors"]["delta"] == -1
        assert diff["forward"]["errors"] == {"baseline": 0, "current": 0, "delta": 0}

    def test_does_not_mutate_reports(self):
        baseline = make_report([make_issue()])
        current = make_report([make_issue(code="other")])
        baseline_snapshot = repr(baseline)
        current_snapshot = repr(current)
        compare.compare_reports(baseline, current)
        assert repr(baseline) == baseline_snapshot
        assert repr(current) == current_snapshot

    def test_empty_reports(self):
        result = compare.compare_reports(make_report([]), make_report([]))
        assert result["counts"] == {"added": 0, "fixed": 0, "persisting": 0,
                                    "severity_changed": 0}
        assert result["summary_diff"]["total"]["delta"] == 0


class TestCanonicalOrdering:
    """配对与输出次序与报告内顺序无关（重复 issue 场景）。"""

    def test_persisting_duplicates_swap_order_gives_same_response(self):
        # 同指纹、同级别、不同 message/reason 的两条重复记录；仅交换
        # 当前报告内的顺序，persisting 数组必须逐字一致（旧实现按 FIFO
        # 配对且排序键只含四字段时会输出相反顺序）。
        same = dict(path="#/properties/a", code="required_added", severity="error")
        a = make_issue(message="消息A", reason="原因A", **same)
        b = make_issue(message="消息B", reason="原因B", **same)

        r1 = compare.compare_reports(make_report([dict(a), dict(b)]),
                                     make_report([dict(a), dict(b)]))
        r2 = compare.compare_reports(make_report([dict(b), dict(a)]),
                                     make_report([dict(b), dict(a)]))
        r3 = compare.compare_reports(make_report([dict(a), dict(b)]),
                                     make_report([dict(b), dict(a)]))

        assert r1["counts"]["persisting"] == 2
        assert [i["message"] for i in r1["issues"]["persisting"]] == ["消息A", "消息B"]
        assert r2 == r1
        assert r3 == r1  # 两报告顺序均被打乱，结果仍一致

    def test_full_result_invariant_under_any_permutation(self):
        # 覆盖四类结果与两阶段配对的复合场景：打乱两份报告内 issue 的
        # 任意排列，完整响应（除 baseline/current 概念外）必须完全一致。
        same = dict(path="#/properties/p", code="type_removed", reason="r")
        baseline = [
            make_issue(message="e1", severity="error", **same),
            make_issue(message="e2", severity="error", **same),
            make_issue(message="w1", severity="warning", **same),
            make_issue(path="#/properties/f", code="enum_added",
                       severity="error", message="only-base", reason="rb"),
        ]
        current = [
            make_issue(message="w2", severity="warning", **same),
            make_issue(message="e3", severity="error", **same),
            make_issue(message="w3", severity="warning", **same),
            make_issue(message="w4", severity="warning", **same),
            make_issue(path="#/properties/n", code="const_added",
                       severity="error", message="only-cur", reason="rc"),
        ]
        expected = compare.compare_reports(make_report(baseline),
                                           make_report(current))
        # 级别桶计数：base error×2/warning×1，cur error×1/warning×3
        # -> persisting=2（error、warning 各一），severity_changed=1
        #    （error→warning），fixed=1，added=2
        assert expected["counts"] == {"added": 2, "fixed": 1,
                                      "persisting": 2, "severity_changed": 1}

        perms_baseline = [
            baseline,
            list(reversed(baseline)),
            [baseline[2], baseline[0], baseline[3], baseline[1]],
        ]
        perms_current = [
            current,
            list(reversed(current)),
            [current[3], current[0], current[4], current[2], current[1]],
        ]
        for b in perms_baseline:
            for c in perms_current:
                assert compare.compare_reports(make_report(b), make_report(c)) == expected

    def test_duplicate_records_are_kept_with_counts(self):
        # 规范排序只定次序、不去重：完全相同的重复条目全部保留。
        issue = make_issue(path="#/properties/q", code="required_added")
        baseline = make_report([dict(issue), dict(issue), dict(issue)])
        current = make_report([dict(issue)])
        result = compare.compare_reports(baseline, current)
        assert result["counts"] == {"added": 0, "fixed": 2, "persisting": 1,
                                    "severity_changed": 0}
        assert len(result["issues"]["fixed"]) == 2

    def test_severity_changed_sorted_by_severity_pair_then_message(self):
        # 同指纹的跨级别重复配对：级别组合相同时按 message 规范排序，
        # 与两份报告内的出现顺序无关。
        same = dict(path="#/properties/s", code="integer_required", reason="r")
        baseline = [
            make_issue(severity="error", message="b-high", **same),
            make_issue(severity="error", message="b-low", **same),
        ]
        current = [
            make_issue(severity="warning", message="c-zzz", **same),
            make_issue(severity="warning", message="c-aaa", **same),
        ]
        r1 = compare.compare_reports(make_report(baseline), make_report(current))
        r2 = compare.compare_reports(make_report(list(reversed(baseline))),
                                     make_report(list(reversed(current))))
        assert r1["counts"]["severity_changed"] == 2
        messages = [(i["baseline_severity"], i["current_severity"], i["message"])
                    for i in r1["issues"]["severity_changed"]]
        assert messages == [
            ("error", "warning", "c-aaa"),
            ("error", "warning", "c-zzz"),
        ]
        assert r2 == r1

    def test_added_and_fixed_ties_broken_deterministically(self):
        # added / fixed 中同四字段的重复记录按 severity/message/reason 决胜，
        # 交换报告内顺序输出不变。
        dup = dict(path="#/properties/d", code="required_added", severity="error")
        a = make_issue(message="a", reason="x", **dup)
        b = make_issue(message="b", reason="y", **dup)
        result = compare.compare_reports(make_report([dict(a), dict(b)]),
                                         make_report([]))
        assert [i["message"] for i in result["issues"]["fixed"]] == ["a", "b"]
        result_rev = compare.compare_reports(make_report([dict(b), dict(a)]),
                                             make_report([]))
        assert [i["message"] for i in result_rev["issues"]["fixed"]] == ["a", "b"]


class TestConditionalIssues:
    """条件分支 issue 的指纹与分类。"""

    def test_conditional_path_participates_in_fingerprint(self):
        base = make_issue(path="#/then", code="required_added")
        assert compare.issue_fingerprint(base) != compare.issue_fingerprint(
            make_issue(path="#/else", code="required_added"))
        assert compare.issue_fingerprint(base) != compare.issue_fingerprint(
            make_issue(path="#", code="required_added"))

    def test_conditional_severity_change_classified(self):
        from app.compat import HEURISTIC_SUFFIX
        reason = "旧版中 'y' 可以缺省，新版 required 包含该字段"
        baseline = make_report([make_issue(
            path="#/then", code="required_added", severity="warning",
            reason=reason + HEURISTIC_SUFFIX)])
        current = make_report([make_issue(
            path="#/then", code="required_added", severity="error", reason=reason)])
        result = compare.compare_reports(baseline, current)
        assert result["counts"]["severity_changed"] == 1
        changed = result["issues"]["severity_changed"][0]
        assert changed["path"] == "#/then"
        assert changed["baseline_severity"] == "warning"
        assert changed["current_severity"] == "error"
