# 阶段工作流 4.2.6 测试边界设计与复刻指南

## 改动背景

4.2.5 已经规定按改动影响选择测试。部分入口和阶段参考仍使用“严格审阅”“独立复做关键验收”等宽泛表述。实现者已经验证过的路径，可能在审阅、修复回合和最终验收时被各自再跑一遍。4.2.6 将执行条件收进同一份 [测试策略](../skills/project-phase-workflow/references/testing.md)，各阶段引用同一规则。

本次发布包括阶段工作流技能、测试边界规则及本指南。桥接版本为 1.3.2。本指南的全局规则片段供另一台电脑按需合并进已有配置。

## 如何决定要测什么

1. **确定本轮通过条件。** 从批准的验收条件（AC）、实际改动和受影响的已有行为出发。写清需要验证的用户操作或系统行为、手头已有证据以及完成条件。简单任务写在任务单即可。
2. **验证直接相关的行为。** 修复优先验证原问题及受影响回归。界面改动优先实际操作并观察反馈。共享状态、持久化、权限、并发和外部副作用，按实际影响决定是否扩大。项目规定和用户明确要求的强制检查照常执行。
3. **复用证据。** 核对被测版本、环境、检查范围、结果，以及相关代码、依赖、数据和测试夹具是否变化。局部变化只使受影响结论失效。同一份有效结果可以支持多个验收项。报告继续保留失败、阻塞和未测项。
4. **有依据才追加验证。** 补反例或重跑前，明确受支持的场景、相关改动或具体现象，以及结果可能如何改变交付判断。需要扩大时先测受影响模块或核心流程。全量用于既定检查点、局部无法可靠覆盖的广泛影响，或已知全量比可靠筛选更省总耗时的情况。
5. **证据够用就收尾。** 本轮必验项均有有效证据且无影响交付的未解决问题，结束测试，按约定交回。小步提交、交回验收表、自检切换到审阅或改报告，均不会刷新已有证据。强制项失败或缺关键证据时写明具体问题，按原因修复、补验或报告阻塞。

| 实际情况 | 按新规则处理 |
| --- | --- |
| 修改设置面板的布局和保存反馈 | 打开页面完成保存，检查当前布局、保存反馈及相邻交互。复用仍有效的业务结果。 |
| 只修正一个测试辅助函数 | 检查它能否区分成功与失败，运行使用它的受影响测试。若调用关系清楚、没有其他发布要求，相关结果足以收尾。 |
| 实现者提供了与当前版本对应的运行证据 | 审阅者阅读原要求、真实差异和测试证据，独立判断覆盖是否充分。仅对约定独立实跑的检查和有实质缺口的行为补测。 |
| 局部修复后准备发布 | 按最终版本核对所有强制 AC。复用仍有效的结果，补验相关变化和缺少证据的整体流程。发布规则要求全量时执行。 |

## 逐文件改动

下表路径相对于仓库根目录。`strict-review.md` 保留原文件名，便于现有配置继续引用。

| 文件 | 4.2.5 的相关表述 | 4.2.6 的处理 |
| --- | --- | --- |
| [`SKILL.md`](../skills/project-phase-workflow/SKILL.md) | 主入口要求每轮按“严格审阅”检查，另提醒小改无需全量。 | 版本升至 4.2.6。统一引用测试策略，明确证据充分后收尾。 |
| [`references/testing.md`](../skills/project-phase-workflow/references/testing.md) | 已有影响选测与证据复用，追加检查和收尾的边界相对分散。 | 增加“何时收尾”，明确追加测试、独立实跑、全量及既有证据有效性的条件。 |
| [`references/strict-review.md`](../skills/project-phase-workflow/references/strict-review.md) | 要求独立复做关键验收及回归，并检查相邻组件和未覆盖组合。 | 依原要求和实质风险判断，按约定实跑或补必要证据。收尾结论绑定验收范围。有效证据充分时结束。 |
| [`references/phase-0-5.md`](../skills/project-phase-workflow/references/phase-0-5.md) · [`handoff-quality.md`](../skills/project-phase-workflow/references/handoff-quality.md) | 方案和交接说明如何规划相关及综合检查。 | 在计划里写清每批必验行为、何时扩大检查及收尾条件。高成本检查约定具体触发条件。 |
| [`references/phase-1.md`](../skills/project-phase-workflow/references/phase-1.md) · [`phase-2-n.md`](../skills/project-phase-workflow/references/phase-2-n.md) | 各阶段另写了独立复做关键检查。 | 先引用有效证据，再按统一策略补跑，重新验证限于受影响结论。 |
| [`references/final-acceptance.md`](../skills/project-phase-workflow/references/final-acceptance.md) | 要求综合复核及在可用环境走通首次使用。 | 逐项查最终版本证据，补缺口和规定的发布检查。首次使用已有有效证据且说明无实质变化时可复用。 |
| [`references/bridge.md`](../skills/project-phase-workflow/references/bridge.md) | 派发规定必验范围，回传后要求“严格审阅”。 | 在任务或修复意见中写出必验行为、可复用证据及完成条件。全量有具体触发理由，回传后按同一证据标准审阅。 |

`references/phase-0.md` 和 `scripts/last_opencode_model.py` 随完整技能目录安装。它们继续负责需求阶段说明和读取上次实现模型。

## 在另一台 Windows 电脑复刻

以下以本次使用的共享技能目录 `.agents/skills` 为例。如果另一台电脑的默认技能索引已从其他位置加载 `project-phase-workflow`，请把命令里的技能根目录指向实际安装位置。Codex 与 OpenCode 共用同一个已发现的版本即可。

### 1. 取得发布仓库

```powershell
git clone https://github.com/joeyl3526-cloud/project-phase-workflow.git
Set-Location project-phase-workflow
```

已有本仓库时，在该仓库根目录确认本地修改已保存，再使用 `git pull --ff-only` 更新。核对 `skills/project-phase-workflow/SKILL.md` 中的 `metadata.version`。本文描述的是 `4.2.6`。

### 2. 备份并安装技能

在仓库根目录运行以下 PowerShell。已有同名技能时，备份保存到技能搜索目录之外。脚本将规则、参考文件和现有模型读取脚本复制到同一技能目录。

```powershell
$profilePath = [Environment]::GetFolderPath('UserProfile')
$sourceSkill = (Resolve-Path -LiteralPath 'skills/project-phase-workflow').Path
$installedSkill = Join-Path $profilePath '.agents/skills/project-phase-workflow'

if (Test-Path -LiteralPath $installedSkill -PathType Container) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $backupRoot = Join-Path $profilePath 'phase-workflow-backups'
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    $backupSkill = Join-Path $backupRoot "project-phase-workflow-$stamp"
    Copy-Item -LiteralPath $installedSkill -Destination $backupSkill -Recurse -ErrorAction Stop
    Write-Output "Backup: $backupSkill"
}

New-Item -ItemType Directory -Path $installedSkill -Force | Out-Null
$skillParts = @('SKILL.md', 'references', 'scripts') | ForEach-Object { Join-Path $sourceSkill $_ }
Copy-Item -LiteralPath $skillParts -Destination $installedSkill -Recurse -Force -ErrorAction Stop
Select-String -LiteralPath (Join-Path $installedSkill 'SKILL.md') -Pattern 'version:'
```

### 3. 对齐已有全局入口

先备份需要编辑的 `AGENTS.md`。如果另一台电脑的全局文件已有“严格审阅”“每轮独立复做”“最终验收必须重跑”等旧描述，仅用下面的片段替换对应语句。现有文件中的其他协作规则继续沿用。

**Codex** 通常在 `~/.codex/AGENTS.md` 的阶段工作流章节。使用自定义配置目录时，编辑实际加载的文件。

> GPT 核对阶段 0/0.5 文档完整性，依据原要求、实际差异和证据审阅 OpenCode 成果，并比较有实质价值的实现路径。按 `project-phase-workflow` 的测试策略验证本轮及受影响范围，复用有效证据，验收条件满足后结束测试。全量和独立实跑按项目要求和具体证据缺口判断。

**OpenCode** 在本次配置的 `~/.config/opencode/AGENTS.md` 中，替换共用协作与阶段规则下对审阅的旧说明。

> GPT 按共享技能的 `strict-review.md` 核对本轮原要求、实际差异和证据。实施和审阅均按 `testing.md` 选择检查并复用有效结果，达到本轮验收条件后结束测试。

如果已有项目的任务单写着“每次都跑全量”，先确认它来自项目放行要求、用户要求还是以前派发时附加的惯例。前两种继续执行。第三种按实际影响重新写成本轮必验行为、可沿用证据和测试完成条件。

### 4. 核对启用情况与回退

1. 在新聊天中让 Codex 或 OpenCode 通过默认技能索引加载 `project-phase-workflow`，核对版本为 `4.2.6`，并确认入口可读到 `references/testing.md` 的“何时收尾”和“扩大范围与检查点”。
2. 下一次真实小改时，看任务单是否写清用户操作和完成条件，审阅回执是否引用有效证据，是否在扩大检查时给出具体依据。一次实际项目的结果比单独的规则问答更能说明执行情况。
3. 需要回退时，关闭或结束正在使用新规则的聊天，将当前技能目录移到技能搜索范围以外，把备份恢复为原来的 `project-phase-workflow` 目录，并恢复备份的 `AGENTS.md`。再次开启聊天核对版本。

原机器上已做技能格式校验、23 条相对链接检查和 11 个技能文件一致性核对。模型在真实项目遵守新规则的程度、节省的用量和跨机安装结果仍需实际观察。
