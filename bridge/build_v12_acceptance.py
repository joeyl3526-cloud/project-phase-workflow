"""Export current sources and the user's ten-cycle daily acceptance scope."""
from pathlib import Path
import base64
import json
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'outputs/Python桥接-v1.2'
DEST = Path.home() / '.codex/tools/phase-workflow'
SKILL = Path.home() / '.agents/skills/project-phase-workflow'
from phase_bridge import Bridge, atomic_json, file_digest, now, read_json


def link(name):
    path = OUT / name
    return f'[{name}](<{path.as_posix()}>)'


def daily_use_passed(record, final, policy):
    """Apply the user's recorded cycle requirement to persisted final evidence."""
    if final is None or record.get('model_calls') != 0:
        return False
    count = record.get('cycles_completed', 0)
    if count < policy['minimum_completed_cycles']:
        return False
    cycles = record.get('cycles', [])
    if [item.get('index') for item in cycles] != list(range(count)):
        return False
    if any(item.get('status') != 'PASS' for item in cycles):
        return False
    if record.get('elapsed_seconds', 0) < policy['minimum_elapsed_seconds']:
        return False
    if record.get('checkpoint_gaps_over_30_seconds', 0):
        return False
    if record.get('status') == 'PASS':
        return True
    return (Path(record['workspace']).name == policy['accepted_workspace']
        and record.get('error_code') == policy['accepted_stop_code'])


def main():
    receipt = read_json(OUT / '安装回执.json')
    source = read_json(OUT / '源代码回归测试.json')
    installed = read_json(OUT / '已安装回归测试.json')
    assert source['status'] == installed['status'] == 'PASS'
    test_count = source['tests_run']
    assert source['runtime_sha256'] == installed['runtime_sha256'] == receipt['source_sha256']
    for name, expected in receipt['source_sha256'].items():
        assert file_digest(ROOT / 'bridge' / name) == file_digest(DEST / name) == expected
        shutil.copy2(DEST / name, OUT / name)
    exported = []
    for path in sorted(SKILL.rglob('*')):
        if path.is_file() and path.suffix in ('.md', '.py'):
            target = OUT / '已安装工作流' / path.relative_to(SKILL)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            assert file_digest(path) == file_digest(target), 'Concurrent skill edit during export'
            exported.append({'source': str(path), 'export': str(target), 'sha256': file_digest(path)})
    skill_version = re.search(r'version:\s*"([^"]+)"', (SKILL / 'SKILL.md').read_text(encoding='utf-8-sig')).group(1)
    atomic_json(OUT / '当前文件导出核验.json', {'at': now(), 'skill_version': skill_version,
        'skill_files': exported, 'runtime_files_sha256': receipt['source_sha256'],
        'concurrent_visual_rules_preserved': all(text in (DEST / 'phase_bridge.py').read_text(encoding='utf-8')
            for text in ['有能力时执行视觉检查，能力不足时交付可检查的成果，并明确哪些检查未完成',
                         '视觉任务中，OpenCode 侧多模态模型可承担部分视觉评审，GPT 必须参与视觉评审；交回实际截图或渲染结果供复核。'])})
    bridge = Bridge(DEST / 'bridge-config.json')
    pointer = read_json(ROOT / 'work/bridge-v3/v12-run.json')
    run = bridge.load(pointer['run_id'])
    before = file_digest(bridge.runpath(run['run_id']))
    def unexpected_api(*args, **kwargs):
        raise AssertionError('Reviewed runs must not query later native-session work')
    bridge.api = unexpected_api
    noops = []
    for item in run['history']:
        result = bridge.collect(run['run_id'], item['task_id'], item['callback_id'], item['context_hash'])
        assert result['disposition'] == 'ALREADY_REVIEWED_NOOP'
        noops.append(item['task_id'])
    assert bridge.collect(run['run_id'])['status'] == 'COMPLETED'
    assert file_digest(bridge.runpath(run['run_id'])) == before
    atomic_json(OUT / '已完成流程幂等核验.json', {'at': now(), 'status': 'PASS',
        'run_id': run['run_id'], 'old_callbacks_noop': noops, 'generic_collect': 'COMPLETED',
        'persisted_state_unchanged': True, 'executor_api_calls': 0, 'model_calls': 0})
    rollback = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(ROOT / 'bridge/rollback_v12.py')],
        capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert rollback.returncode == 0
    atomic_json(OUT / '回退预览.json', json.loads(rollback.stdout))
    real = read_json(OUT / '真实往返综合核验.json')
    policy = read_json(OUT / '验收标准.json')
    daily_path = OUT / '日常10轮验收.json'
    endurance_evidence = daily_path if daily_path.is_file() else OUT / '耐久测试.json'
    endurance = read_json(endurance_evidence)
    endurance_workspace = Path(endurance['workspace']).resolve()
    assert endurance_workspace.is_relative_to((ROOT / 'work/bridge-v3/endurance').resolve())
    final_path = endurance_workspace / 'result.json'
    final = read_json(final_path) if final_path.is_file() else None
    interruption_path = OUT / '耐久测试-6a5b7771-中断审阅.json'
    interruption = read_json(interruption_path) if interruption_path.is_file() else None
    interrupted = interruption and interruption['workspace'] == endurance['workspace']
    if final:
        assert final['workspace'] == endurance['workspace']
        assert final['runtime_sha256'] == receipt['source_sha256']
        endurance = final
    endurance_status = 'INTERRUPTED' if interrupted else endurance['status']
    atomic_json(ROOT / 'work/bridge-v3/endurance-current.json', {key: endurance.get(key)
        for key in ('started_at', 'pid', 'workspace', 'runtime_sha256', 'harness_sha256', 'fixture_sha256')}
        | {'status': endurance_status})
    for source_path, key, name in [(ROOT / 'bridge/endurance_v12.py', 'harness_sha256', 'harness-used.py'),
                                   (ROOT / 'bridge/fixture_opencode.py', 'fixture_sha256', 'fixture-used.py')]:
        if file_digest(source_path) == endurance.get(key):
            shutil.copy2(source_path, Path(endurance['workspace']) / name)
    preflight = read_json(OUT / '耐久预检.json')
    assert preflight['status'] == 'PASS' and preflight['cycles_completed'] >= policy['minimum_completed_cycles']
    assert preflight['runtime_sha256'] == receipt['source_sha256']
    process = read_json(OUT / '协调进程联调.json')
    assert all(item['status'] == 'PASS' for item in process['checks'])
    assert endurance['runtime_sha256'] == receipt['source_sha256']
    daily_pass = daily_use_passed(endurance, final, policy)
    startup_path = OUT / 'Windows登录重启-观察.json'
    startup = read_json(startup_path) if startup_path.is_file() else {}
    manual = read_json(OUT / '登录启动预检.json')
    managed = read_json(OUT / '计划任务自动接管验收.json')
    visible = read_json(OUT / '可见CLI实时连接验收.json')
    restore = read_json(OUT / '实际回退恢复演练.json')
    model = read_json(OUT / '模型绑定核对.json')
    assert managed['status'] == visible['status'] == restore['status'] == 'PASS'
    startup_pass = (startup.get('actual_reboot') == 'PASS_OBSERVED'
        and bool(startup.get('checks')) and all(startup['checks'].values())
        and manual['runtime_version'] == source['version'] and manual['idle_active_runs'] == 0)
    desktop = read_json(OUT / '用户可见性反馈.json').get('post_reboot_desktop_review', {})
    desktop_history_pass = desktop.get('status') == 'PASS_HISTORY' and len(desktop.get('rounds', [])) == 3
    cli_feedback = read_json(OUT / '用户可见性反馈.json').get('cli_user_confirmation', {})
    cli_confirmed = cli_feedback.get('status') == 'PASS_USER_CONFIRMED'
    rows = [
        ('M01', '缺失/异常成果转明确故障并通知，监测健康可定位', 'PASS',
         f'原缺失成果、畸形回执、非法锁路径、Windows临时读占用等均有回归；源码/安装版各{test_count}项通过。', '源代码回归测试.txt', '永久权限或磁盘故障仍需人工处理。'),
        ('M02', '回传不明确先核对，接收确认与审阅去重', 'PASS',
         '三次真实回传分别接收ACK并完成审阅；三条旧回传及普通collect均不改已完成状态。', '已完成流程幂等核验.json', '原生队列本身不按客户端ID去重；依赖桥接发送状态与接收幂等。'),
        ('M03', '暂停期间完成/失败/中断可恢复审阅，保持A/B边界', 'PASS',
         '恢复通知、中断只允许BLOCKED、协调索引保留至审阅均通过；中断不自动重发。', '已安装回归测试.txt', '中断后的重新执行范围需新的用户决定。'),
        ('M04', '连接失败刷新服务地址/schema，未知修改不盲目重试', 'PASS',
         '回环HTTP/端口失效回归和两种真实Desktop/后台冷启动通过。', '真实冷启动验收.json', '仅限本机已测V2接口；服务升级不兼容时停止派发。'),
        ('M05', '单实例、跨阶段交接、崩溃恢复不重复派发', 'PASS',
         '5项实际协调进程检查、4项计划任务接管检查通过；退出后约55秒自行恢复，原提示提交次数保持1。', '计划任务自动接管验收.json', '使用同一计划任务每分钟触发接管；不承诺瞬时恢复，依赖用户登录会话和Windows任务服务。'),
        ('M06', '两阶段与一次修复自动接回原聊天，关键上下文落地', 'PASS',
         '3次实际DeepSeek派发；原成果12项先通过再受控注错，修复13/13，最终15/15，同一会话COMPLETED。', '真实往返综合核验.json', '真实闭环在终态、派发锁及读重试补丁前完成；新修订另经本地回归和进程检查。上下文包由当前聊天整理。'),
        ('M07', 'Desktop按需拉起，开机并登录后单实例自动待命', 'PASS' if startup_pass else 'PARTIAL',
         '已有实际开机/登录及两种Desktop冷启动证据；当前任务预检通过。按用户批准的回退方案，自动打开同一HTTP服务及精确会话的可见CLI，实时标题事件实测通过。' if startup_pass else '实际开机证据或当前启动预检不足。',
         'Windows登录重启-观察.json' if startup_pass else 'Windows登录重启-基线.json',
         'Desktop原生精确导航未实现，CLI提供自动可见入口；开机证据来自1.2.1，当前定时接管另经实测。' if startup_pass else '开机并登录后的实际自启待验证。'),
        ('V08', f"日常使用尺度达到{policy['minimum_completed_cycles']}轮", 'PASS' if daily_pass else 'IN PROGRESS' if endurance_status == 'RUNNING' and final is None else 'FAIL',
         f"原版11轮约24分51秒后按用户要求停止；最终1.2.2另完成{endurance['cycles_completed']}轮压缩合成复验，{endurance.get('elapsed_seconds', 0):.2f}秒，外部模型调用0次。", endurance_evidence.name,
         '当前放行采用10轮日常尺度；压缩复验不证明4小时或24小时连续运行。' if daily_pass else '待核对本代最终结果和实际完成轮次。'),
        ('V09', '最终修订下真实DeepSeek流式过程另行观察', 'NOT TESTED',
         '用户此前确认首轮提示与输出可见。重启后GPT逐轮查看了同一专用会话的3轮提示与输出。' if desktop_history_pass else '用户此前确认首轮提示与输出可见，技术上3次派发复用同一会话。',
         '用户可见性反馈.json', '用户已确认CLI会话和已有输出可见；真实模型流式过程仍未重跑。' if cli_confirmed else '本轮只验证CLI实时本地事件，不追加模型调用；最终CLI窗口的用户体验确认仍待回复。'),
        ('V10', '所选DeepSeek路由正确、推理配置可追溯', 'PASS',
         '实际API为deepseek-flash；专用会话variant=default，基础配置reasoningEffort=max、thinking=enabled。', '模型绑定核对.json', '未捕获上游实际请求体；不证明上游实际推理强度，也不保证日期锁定版本。'),
        ('V11', '技能、MCP及插件入口可加载，交付文件一致', 'PASS',
         f'当前技能{skill_version}，导出{len(exported)}份技能文件和5个运行模块；新原生宿主发现10个MCP工具。', '当前文件导出核验.json', '保留用户4.2.0职责边界和既有插件注册；其他项目需各自授权、整理上下文。'),
        ('V12', '升级回退及恢复可执行、历史不变', 'PASS',
         '已实际退回1.2.1并启动，再精确恢复最终1.2.2、配置和计划任务；业务历史哈希未变。', '实际回退恢复演练.json', '完整卸载到1.1未实操，默认预览保留；恢复后须重新加载MCP。'),
    ]
    if cli_confirmed:
        rows.insert(next(i for i, item in enumerate(rows) if item[0] == 'V09') + 1,
            ('V09a', '可见CLI显示工作会话及已有输出', 'PASS',
             '用户实际确认：“' + cli_feedback['answer'] + '”。', '用户可见性反馈.json',
             '本项仅覆盖会话及已有输出；不扩展为真实模型流式过程已验收。'))
    if interruption and not interrupted:
        rows.insert(7, ('V08 夜间测试', '冻结版本连续4小时、100轮', 'FAIL',
            f"{interruption['last_checkpoint'][11:19]} 留下最后检查点，已验证 {interruption['cycles_completed']} 轮和 {interruption['elapsed_seconds']:.1f} 秒，独立最终结果缺失。",
            interruption_path.name, '历史中断原因未定。当前日常验收采用用户确认的10轮标准。'))
    stamp = now()
    report = {'at': stamp, 'overall_status': 'PASS_DAILY_SCOPE' if daily_pass and startup_pass else 'PARTIAL',
        'implementation_status': 'COMPLETE',
        'user_acceptance': 'PASS_USER_CONFIRMED for CLI session and existing outputs only; final-revision live model streaming not tested' if cli_confirmed else 'Final CLI visibility confirmation pending; prior user feedback retained',
        'cli_display_user_acceptance': 'PASS_USER_CONFIRMED' if cli_confirmed else 'PENDING',
        'scope': 'M01-M07 technical daily scope, user-authorized visible CLI fallback and ten-cycle criterion; current-chat self-review. Final-revision live inference capture is not included.',
        'runtime_version': source['version'], 'runtime_sha256': receipt['source_sha256'], 'skill_version': skill_version,
        'tests_source': source['tests_run'], 'tests_installed': installed['tests_run'],
        'real_model_calls': real['model_calls'], 'real_run_status': real['status'],
        'boot_logon_status': 'PASS' if startup_pass else 'PARTIAL',
        'desktop_history_status': 'PASS' if desktop_history_pass else 'PARTIAL',
        'endurance_status': endurance_status,
        'daily_use_endurance_status': 'PASS' if daily_pass else 'PARTIAL',
        'daily_use_cycles_required': policy['minimum_completed_cycles'],
        'daily_use_cycles_completed': endurance['cycles_completed'],
        'endurance_started_at': endurance['started_at'],
        'closeout_external_model_calls': 0,
        'endurance_duration_target_seconds': policy['minimum_elapsed_seconds'],
        'items': [dict(zip(['id','standard','status','actual','evidence','remaining_gap'], row)) for row in rows]}
    atomic_json(OUT / '综合验收.json', report)
    lines = ['# Python桥接最终验收清单', '', f'更新：{stamp}（Asia/Shanghai）。技术结论：{report["overall_status"]}；代码、安装和交付收尾完成，用户体验验收单列。', '',
        '成果范围：已批准M01～M07、用户批准的可见CLI回退及10轮日常尺度。纯Python标准库核心；当前聊天自审，用户桌面反馈单列。闲时资源按用户决定视为过关。保留所有未测和历史失败项。', '',
        f"当前桥接{source['version']}，源码/安装模块各{test_count}项回归通过，5项真实协调进程检查通过。真实DeepSeek两阶段加一次受控修复均在匹配回传到达后审阅，终点COMPLETED，无后续派发。", '',
        '| 验收项 | 通过标准 | 实际结果与状态 | 证据 | 剩余缺口 |', '|---|---|---|---|---|']
    for identifier, standard, status, actual, evidence, gap in rows:
        lines.append(f'| {identifier} | {standard} | **{status}**：{actual} | {link(evidence)} | {gap} |')
    lines.extend(['', '## 被测版本和证据范围', '',
        '真实模型正常闭环在先前修订完成。最终1.2.2增加计划任务统一归属、每分钟接管、同服务认证的可见CLI和Codex官方应用升级后的路径定位；保留用户4.2.0前端职责规则。当前修订另经源码/安装回归、实际协调进程及完成run的幂等核验。旧模型结果未被写成新修订重跑结果。', '',
        '本机Desktop已更新到2.2.7，先前冷启动也包含2.2.6证据。全局禁用模型策略保留。API实际名称deepseek-flash，max为已核对配置，不宣称日期版本固定或推理强度已端到端追踪。', '',
        '## 长时间运行与潜在问题', '',
        '1. 上一轮长测23轮、约55分钟后PermissionError未捕获栈。新预检第6轮复现，定位到状态JSON打开读取；真实Windows独占句柄能复现同类拒绝，已加最多5次、累计约0.5秒等待，持续拒绝仍报错。又在第40轮发现健康/接收更新导致审阅版本误判，已改业务状态核对并保留暂停/成果变化保护。原长测具体占用者及是否同因未证明；失败报告保留，压缩100轮不能替代长测。',
        '2. 当前聊天持续活跃、宿主关闭、额度中断或系统休眠都会影响接续时间。派发后及时交回，让队列在空闲边界接回；入队不表示已接收或已审阅。',
        '3. 上下文采用明确整理的任务包。文件指纹能校验版本，语义仍靠成果检查；用户新修正须先落实到有效依据及下一次上下文。',
        '4. Desktop新专用会话首次查看仍需选择；当前已采用批准的CLI回退自动打开精确会话，并实测实时本地事件。保持该工作流窗口在专用会话，手动切换会影响你看到的内容。最终修订未追加真实模型过程录像。',
        '5. allowed_roots只是桥接路径检查，OpenCode命令执行仍依赖应用权限，不是OS沙箱。当前接口为本机被测版本；应用升级后应先核对兼容性。',
        '6. 当前MCP和登录均由同一普通用户计划任务拉起。单用RestartOnFailure在本机未通过，已增加每分钟触发、IgnoreNew及单实例锁，不增加常驻进程。自动接管约一分钟；维护时先禁用本任务，防止定时器重新启动。任务服务或用户会话不可用时无法保证恢复。', '',
        f"日常验收采用用户确认的{policy['minimum_completed_cycles']}轮标准，实际完成{endurance['cycles_completed']}轮。", '',
        '进度与最终结果见本代记录。' + link(endurance_evidence.name), '',
        *(['夜间测试的最后检查点早于用户报告的06:50断网，也早于Windows记录的08:07异常关机。原测试缺少独立运行日志，停止原因未定。复验由普通用户一次性Windows任务启动，标准输出和错误输出保存在工作区。' + link(interruption_path.name), ''] if interruption else []),
        '## 使用、恢复与实际OS验收', '',
        ('本次异常关机后的开机与登录已完成只读验收。' + link('Windows登录重启-观察.json')) if startup_pass else '开机与登录的实际观察仍待补证。', '',
        '日常调用、回传ACK、A/B、中断处理及登录后采集顺序见使用说明。' + link('使用与恢复.md'), '',
        '实际升级回退及恢复已演练；完整卸载仍只预览。模块备份位于安装回执所列用户目录，含原始配置的备份保留在原处。', '',
        '技能和运行文件导出见' + link('当前文件导出核验.json') + '；审阅报告/固定上下文/最终合成代码归档在真实往返成果目录。'])
    (OUT / '验收清单.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    # Only the known local service credential and obvious key literals; no other-account token inventory.
    service = Path(bridge.config.get('service_config', Path.home() / '.config/opencode/service.json'))
    password = read_json(service)['password']
    assert isinstance(password, str) and len(password) >= 8
    known = [password.encode(), base64.b64encode(('opencode:' + password).encode())]
    matches = []
    count = 0
    for path in OUT.rglob('*'):
        if not path.is_file() or path.name == '交付目录凭据扫描.json':
            continue
        data = path.read_bytes()
        count += 1
        if any(value in data for value in known):
            matches.append({'file': str(path.relative_to(OUT)), 'kind': 'KNOWN_LOCAL_SERVICE_CREDENTIAL'})
        if re.search(rb'\bsk[-_][A-Za-z0-9_-]{20,}\b', data):
            matches.append({'file': str(path.relative_to(OUT)), 'kind': 'POSSIBLE_LITERAL_API_KEY'})
    scan = {'at': now(), 'status': 'PASS_SCOPED' if not matches else 'FAIL',
        'scope': 'Current v1.2 delivery directory only; exact current local-service password/Basic value and obvious sk literals.',
        'files_checked': count, 'matches': matches, 'raw_credentials_output': False,
        'limits': 'Not a full historical-chat, all-provider-token or user-backup audit.'}
    atomic_json(OUT / '交付目录凭据扫描.json', scan)
    assert not matches, 'Potential credential in delivery; inspect privately before handoff'
    if (OUT / '最终版本真实流式补测.json').is_file():
        from sync_live_v122_evidence import main as sync_live
        sync_live()
        report = read_json(OUT / '综合验收.json')
    print(json.dumps({'overall': report['overall_status'], 'skill': skill_version, 'exported_skill_files': len(exported),
        'tests_each': source['tests_run'], 'real_run': real['status'], 'endurance': endurance_status,
        'scan': scan['status'], 'report': str(OUT / '验收清单.md')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
