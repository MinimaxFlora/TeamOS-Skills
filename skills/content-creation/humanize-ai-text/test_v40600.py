# -*- coding: utf-8 -*-
"""test_v40600 — v4.6.0 中文密度特征镜像 + compare 分块降级版。

覆盖：
  1. CN 密度信号：zh_paraopen（段落开头公式占比 ≥40% 且 ≥3 段）、
     zh_dash（「——」≥4/千字且 ≥400 字 ≥3 处）、干净中文零触发、
     短文门槛、_zh_opener 不误伤正当学术开场
  2. patterns_zh 扩容：新词命中、zh_opening 增补支（随着…的发展）
  3. compare 大文件：>1M 走 scan_chunked（源码断言）、降级注记、
     --exit-verdict 降级 fail-closed exit 2
  4. 版本锚与文档接线
"""
import json
import os, sys, tempfile, subprocess
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)
import hxt_core
PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  " + detail) if detail and not cond else ""))
def run_cli(args):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable] + args, capture_output=True,
                          text=True, encoding="utf-8", cwd=ROOT, env=env, timeout=600)

_FILL = "研究设计覆盖三个中心与两个随访时点，方案已在注册库登记，随访由专职人员完成。"


def _zh_paper(openers, body_mult=8):
    out = []
    for i, op in enumerate(openers):
        out.append(op + _FILL * body_mult)
    return "\n\n".join(out)


def test_cn_density():
    # 段落开头公式化：5 段中 4 段公式开场（80%）触发
    r = hxt_core.scan(_zh_paper([
        "随着深度学习的发展，该问题被重新审视。",
        "近年来，队列证据不断积累。",
        "为了回答上述争议，我们设计了前瞻性研究。",
        "针对这一缺口，本研究纳入多中心样本。",
        "两组间的基线特征均衡可比，差异均无统计学意义。"]))  # 第 5 段内容起句
    check("zhparaopen.80pct触发", "zh_paraopen" in r["categories"],
          json.dumps(r.get("stats", {}).get("zh_para_open_ratio")))
    # 干净中文（段落内容起句）零触发
    r0 = hxt_core.scan(_zh_paper([
        "两组间的基线特征均衡可比，差异均无统计学意义。",
        "随访期间共记录终点事件 28 例，其中治疗组 11 例。",
        "亚组分析显示疗效在 65 岁以上人群中更为显著。",
        "安全性终点两组无显著差异，未发生治疗相关死亡。",
        "敏感性分析结果与主分析一致，结论稳健。"]))
    check("zhparaopen.干净不触发", "zh_paraopen" not in r0["categories"],
          json.dumps(r0.get("stats", {}).get("zh_para_open_ratio")))
    # 短文门槛：段落不足 5 段不触发
    r1 = hxt_core.scan("随着时间推移，指标改善。\n\n为了验证，我们重复实验。\n\n针对争议，补充了对照。\n\n在压力背景下，结果一致。")
    check("zhparaopen.短文门槛", "zh_paraopen" not in r1["categories"])
    # zh_dash：高密度触发
    r2 = hxt_core.scan(("结果显著——且安全性良好——" * 20) + "正文填充。" * 120)
    check("zhdash.高密度触发", "zh_dash" in r2["categories"],
          json.dumps(r2.get("stats", {}).get("zh_dash_density")))
    # 低密度不触发（一两处正当插入）
    r3 = hxt_core.scan("本研究——一项多中心前瞻性队列——纳入 120 例患者。" + "正文填充。" * 120)
    check("zhdash.正当插入不触发", "zh_dash" not in r3["categories"],
          json.dumps(r3.get("stats", {}).get("zh_dash_density")))
    # 数值区间不算破折号
    r4 = hxt_core.scan("随访 2018——2020 年间。" + "正文填充。" * 130)
    check("zhdash.数值区间豁免", (r4.get("stats", {}).get("zh_dash_count") or 0) == 0)


def test_zh_patterns():
    # 新词命中
    r = hxt_core.scan("该方案提供一站式服务，实现了深度融合。" + "正文填充。" * 30)
    voc = r["categories"].get("zh_jargon", {})
    samples = " ".join(voc.get("samples", []))
    check("zhpatterns.新词命中", "一站式" in samples and "深度融合" in samples, samples[:80])
    # zh_opening 增补支
    r2 = hxt_core.scan("随着人工智能的发展，方法不断演进。" + "正文填充。" * 30)
    cats = [c for c in r2["categories"]]
    check("zhpatterns.zh_opening随随着", "zh_opening" in cats, str(cats))


def test_compare_chunked():
    # 源码接线断言
    src = open(os.path.join(SCRIPTS, "compare.py"), encoding="utf-8").read()
    check("cmpchunked.源码接线", "scan_chunked(txt, degrade=True, **kw)" in src
          and "_deg_o = ro.get(\"degraded_chunks\")" in src)
    # 引擎级：degrade=True 部分降级出结果且带标记（compare 消费同一契约）
    from unittest import mock
    real_scan = hxt_core.scan
    state = {"n": 0}
    def flaky(segment, **kw):
        state["n"] += 1
        if state["n"] <= 2:
            raise ValueError("扫描超时（模拟）")
        return real_scan(segment, **kw)
    big = ("前后对照正文。" * 200000)  # >1M
    with mock.patch.object(hxt_core, "scan", flaky):
        r = hxt_core.scan_chunked(big, degrade=True)
    check("cmpchunked.引擎契约", len(r["degraded_chunks"]) >= 1 and r["score"] >= 0)
    # CLI：大文件 compare 不超预算炸（--exit-verdict 可能 2=降级拦停，合法）
    with tempfile.TemporaryDirectory() as td:
        fo = os.path.join(td, "o.txt"); fn = os.path.join(td, "n.txt")
        open(fo, "w", encoding="utf-8").write("前后对照正文。" * 300000)
        open(fn, "w", encoding="utf-8").write("前后对照改文。" * 300000)
        r = run_cli([SCRIPTS + "/compare.py", fo, fn, "--exit-verdict"])
        check("cmpchunked.大文件不崩", r.returncode in (0, 2, 3),
              "rc=%d err=%s" % (r.returncode, (r.stderr or "")[-100:]))


def test_version_and_docs():
    check("ver.__version__", hxt_core.__version__ == "4.6.0", hxt_core.__version__)
    zh = open(os.path.join(ROOT, "SKILL_ZH.md"), encoding="utf-8").read()
    en = open(os.path.join(ROOT, "SKILL.md"), encoding="utf-8").read()
    check("doc.ZH_v46", "v4.6" in zh)
    check("doc.EN_v46", "v4.6" in en)
    fs = open(os.path.join(ROOT, "references/feature_status.md"), encoding="utf-8").read()
    check("doc.feature_status_v46", fs.count("v4.6") >= 2)


def test_audit_round2():
    # P1-1：跨 1M 阈值分数不变（scan_chunked eff_lang 修复——同文本两路同分）
    same = ("This cohort study enrolled participants across three centers. " * 14000)  # ~1.1M
    r_small = hxt_core.scan(same[:900_000])
    r_chunk = hxt_core.scan_chunked(same, degrade=False)
    check("audit.跨阈值同分", abs(r_small["score"] - r_chunk["score"]) <= 3,
          "small=%s chunk=%s" % (r_small["score"], r_chunk["score"]))
    check("audit.chunk_lang_en", r_chunk.get("lang") == "en", str(r_chunk.get("lang")))
    # P1-2：「在…背景下」主流形态命中 opener
    r = hxt_core.scan(_zh_paper([
        "在数字化背景下，本研究重新审视该问题。",
        "近年来，队列证据不断积累。",
        "为了回答上述争议，我们设计了前瞻性研究。",
        "两组间的基线特征均衡可比，差异均无统计学意义。",
        "随访期间共记录终点事件 28 例，其中治疗组 11 例。"]))
    check("audit.背景下命中", (r.get("stats", {}).get("zh_para_open_hits") or 0) >= 3,
          json.dumps(r.get("stats", {}).get("zh_para_open_hits")))
    # P2-2：裸「随着温度升高」（方法节正常表述）不算公式开场
    r2 = hxt_core.scan(_zh_paper([
        "随着温度升高，反应速率加快。",
        "随着催化剂用量增加，产率提高。",
        "随着反应时间延长，副产物减少。",
        "基线特征均衡可比，差异无统计学意义。",
        "随访终点事件治疗组 11 例，对照组 15 例。"]))
    check("audit.裸随着不计", (r2.get("stats", {}).get("zh_para_open_hits") or 0) == 0,
          json.dumps(r2.get("stats", {}).get("zh_para_open_hits")))
    # P1-3：docx 单换行切段——单 \n 分隔段落触发段落信号
    single_nl = "\n".join([
        "随着深度学习的发展，该问题被重新审视。" + _FILL,
        "近年来，队列证据不断积累。" + _FILL,
        "为了回答上述争议，我们设计了前瞻性研究。" + _FILL,
        "针对这一缺口，本研究纳入多中心样本。" + _FILL,
        "两组间的基线特征均衡可比，差异无统计学意义。" + _FILL])
    r3 = hxt_core.scan(single_nl)
    check("audit.单换行切段触发", "zh_paraopen" in r3["categories"],
          json.dumps(r3.get("stats", {}).get("zh_para_open_ratio")))
    # 端到端：docx 输入（w:p 单换行）路径
    with tempfile.TemporaryDirectory() as td:
        docx = os.path.join(td, "p.docx")
        hxt_core.write_docx(single_nl, docx)
        text = hxt_core._extract_docx(docx)
        r4 = hxt_core.scan(text)
        check("audit.docx路径触发", "zh_paraopen" in r4["categories"],
              json.dumps(r4.get("stats", {}).get("zh_para_open_ratio")))


def main():
    for fn in (test_cn_density, test_zh_patterns, test_compare_chunked,
               test_audit_round2, test_version_and_docs):
        print("── %s" % fn.__name__)
        fn()
    print("\n通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
        sys.exit(1)


if __name__ == "__main__":
    main()
