=============================================================
  moways-docx v2.7.4 — 博维咨询 Word 报告自动排版技能
  适用平台：WorkBuddy
=============================================================

本技能依据《博维咨询 Word 版报告行文排版标准 V2.6-20250920》
及国标《GB/T 15834—2011 标点符号用法》开发，提供三种功能：

  1. 自动排版  —— 一键按博维标准格式化 Word 文档
  2. 排版审查  —— 逐项检查格式合规性，输出问题清单
  3. 中文标点修正 —— 自动修复 AI 写作常见的半角/"AI 味"标点


一、安装步骤
-------------------------------------------------------------

  步骤 1：把 moways-docx/ 整个文件夹放到以下位置：

      ~/.workbuddy/skills/moways-docx/

  最终目录结构应为：

      ~/.workbuddy/skills/moways-docx/
      ├── README.txt              ← 就是这个文件
      ├── SKILL.md
      ├── scripts/
      │   ├── punctuation.py
      │   ├── format_word.py
      │   └── audit_word.py
      └── references/
          └── punctuation_GB_T_15834-2011.md

  步骤 2：安装 Python 依赖（只需首次执行一次）：

      pip install python-docx lxml

  步骤 3：重启 WorkBuddy，技能即自动生效。

  当在对话框中提到"排版""格式""Word""标点"等关键词时，
  技能会自动触发。

  v2.7.4 起，宽表格默认保留纵向页面并自动适应页面宽度，
  不再自动切换为横向页，避免表标题和表格被拆到不同页面。
  只有明确需要横向表格页时，才使用参数：

      --landscape-wide-tables


二、手动触发
-------------------------------------------------------------

  你也可以在左侧"专家"面板的 Skill 列表中找到
  "moways-docx" 并手动使用。


三、常用脚本参数
-------------------------------------------------------------

  自动排版：

      python scripts/format_word.py input.docx output.docx \
        --cover --toc --page-num-style dash

  关闭中文标点自动修正：

      python scripts/format_word.py input.docx output.docx --no-fix-punctuation

  仅在明确需要宽表格横向页时启用：

      python scripts/format_word.py input.docx output.docx --landscape-wide-tables

  排版审查：

      python scripts/audit_word.py input.docx --strict

  v2.7.4 审查项新增：
    - 封面 / 目录 / 正文分页边界
    - 表标题与表格是否紧邻
    - 表标题是否设置与下段同页


四、卸载方法
-------------------------------------------------------------

  删除以下目录即可：

      rm -rf ~/.workbuddy/skills/moways-docx/


五、版本历史
-------------------------------------------------------------

  v2.7.4  (2026-06)  WorkBuddy 兼容补丁：
                     修复封面/目录分页边界；
                     宽表格默认不再自动横向分节；
                     新增 --landscape-wide-tables 显式横向参数；
                     扩展表标题识别；
                     audit 增加分页边界与表标题绑定检查
  v2.7.1  (2026-06)  WorkBuddy 兼容版本
  v2.7    (2026-05)  新增国标 GB/T 15834—2011 标点规则强约束
  v2.6    (2025-09)  博维 Word 排版标准 V2.6 基础版


=============================================================
  博维管理咨询（MOWAYS）
=============================================================
