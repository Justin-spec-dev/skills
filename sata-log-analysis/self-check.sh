#!/usr/bin/env bash
# sata-log-analysis Skill 自检脚本
# 验证：1) 交付文件齐全 2) SKILL.md 内部引用有效 3) frontmatter 完整
#       4) 样例日志内容自洽 5) evals.json 为合法 JSON 且引用的文件存在
set -u
cd "$(dirname "$0")"
fail=0

check() { # check <描述> <条件命令...>
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "PASS  $desc"
  else
    echo "FAIL  $desc"; fail=1
  fi
}

echo "== 1. 交付文件齐全 =="
for f in SKILL.md README.md \
         references/log-fields.md references/eh-flow.md references/report-template.md \
         examples/sample-ata-timeout-eh.log examples/sample-sata-pmp-failure.log \
         examples/sample-report.md evals/evals.json dist/sata-log-analysis.generic.md; do
  check "存在: $f" test -s "$f"
done

echo "== 2. SKILL.md 内部引用 =="
for ref in $(grep -o 'references/[a-z-]*\.md' SKILL.md | sort -u); do
  check "引用有效: $ref" test -f "$ref"
done

echo "== 3. SKILL.md frontmatter =="
check "含 name 字段"        grep -q '^name: sata-log-analysis' SKILL.md
check "含 description 字段" grep -q '^description:' SKILL.md
check "frontmatter 以 --- 开头" bash -c "head -1 SKILL.md | grep -qx -- '---'"

echo "== 4. 样例日志自洽 =="
check "样例1 含 timeout Emask"   grep -q 'Emask 0x4 (timeout)' examples/sample-ata-timeout-eh.log
check "样例1 含 frozen"          grep -q 'frozen' examples/sample-ata-timeout-eh.log
check "样例1 含 EH complete"     grep -q 'EH complete' examples/sample-ata-timeout-eh.log
check "样例1 含内核版本行"       grep -q 'Linux version' examples/sample-ata-timeout-eh.log
check "样例2 含 Port Multiplier" grep -q 'Port Multiplier' examples/sample-sata-pmp-failure.log
check "样例2 含 ATA bus error"   grep -q 'ATA bus error' examples/sample-sata-pmp-failure.log
check "样例2 含 PMP 子端口"      grep -q 'ata2.01' examples/sample-sata-pmp-failure.log

echo "== 5. 示例报告结构 =="
for sec in '## 1. 分析结论' '## 3. 故障时间线' '## 5. libata EH 流程分析' \
           '## 7. 根因推测' '## 9. 信息缺口' '## 10. 最终判断'; do
  check "示例报告含章节: $sec" grep -qF "$sec" examples/sample-report.md
done

echo "== 6. evals.json =="
check "evals.json 是合法 JSON" python3 -c "import json;json.load(open('evals/evals.json'))"
for f in $(python3 -c "
import json
for e in json.load(open('evals/evals.json'))['evals']:
    for f in e.get('files', []): print(f)"); do
  check "evals 引用文件存在: $f" test -f "$f"
done

echo "== 7. 通用导出版 =="
check "通用版含报告模板"   grep -q 'SATA 故障分析报告' dist/sata-log-analysis.generic.md
check "通用版含 SError 表" grep -q 'DISPARITY' dist/sata-log-analysis.generic.md

echo
if [ "$fail" -eq 0 ]; then echo "全部检查通过。"; else echo "存在失败项，见上方 FAIL。"; exit 1; fi
