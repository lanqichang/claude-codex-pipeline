#!/usr/bin/env python3
"""流水线模式的派单器（命令名 cxp）。

Claude 当指挥：拆任务、选档、派评委、裁决返工；本脚本负责一切不需要判断力的活：
后台调用本机 Codex CLI、零 token 监工、改动对账、机器验收、首次失败自动返工、
生成评审包、换档、通知。全程不调用任何 Claude 模型。

常用：
  cxp go --title 标题 --cwd 目录 --tier normal --verify "命令" <<'EOF' 任务单 EOF
  cxp fix <id> <<'EOF' 返工要点 EOF        # 自动附上评委完整报告路径
  cxp status / cxp pending / cxp stop / cxp close <id> --verdict "🟢 通过"
  cxp models [--sync]                       # 当前三档对应的 Codex 型号
"""
import argparse
import difflib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Codex 的位置：环境变量 CODEX_BIN > PATH 里的 codex > ChatGPT 桌面版自带的 Codex
APP_CODEX = '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
CODEX = os.environ.get('CODEX_BIN') or shutil.which('codex') or APP_CODEX
ROOT = Path(os.environ.get('PIPELINE_HOME') or Path.home() / '.local/share/codex-pipeline')
DENY_FILE = Path.home() / '.config/codex-pipeline/deny.txt'   # 每行一个路径片段，命中即拒绝派单
CONFIG = Path.home() / '.codex/config.toml'
HERE = Path(__file__).resolve().parent
TIERS_FILE = HERE / 'tiers.json'                               # 仓库自带的默认档位
USER_TIERS = Path.home() / '.config/codex-pipeline/tiers.json'  # cxp models --sync 的结果写这里，优先于默认
MODELS_CACHE = Path.home() / '.codex/models_cache.json'

MAX_DONE_ROUNDS = 4        # R1 执行 + 最多 3 轮返工
POLL = int(os.environ.get('CODEX_DISPATCH_POLL', '30'))
LOOP_REPEAT = 6            # 同一命令「连续」重复且期间无任何改动 = 原地打转
DIFF_LINES = 300           # 评审包 diff 总行数上限
BUDGET = 6600              # 单次后台调用总时长预算（秒），配合 Bash timeout 7200000 毫秒
SNAP_LIMIT = 30000         # 工作目录文件数超过它就不做全量对账
BASELINE_MB = 30           # 非 git 目录基线副本上限
TEXT_EXT = {'.md', '.txt', '.py', '.js', '.ts', '.tsx', '.jsx', '.html', '.css', '.json', '.csv',
            '.yml', '.yaml', '.toml', '.sh', '.mjs', '.sql', '.xml', '.svg'}
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'dist', 'build', '.next',
             '.obsidian', '.trash', '.tmp', '.cache'}
TIER_ORDER = ['easy', 'normal', 'hard']
STATE_CN = {'new': '待派', 'running': '进行中', 'awaiting_review': '待评审', 'error': '出错',
            'closed': '已结案', 'interrupted': '已中断'}
ALARMS = {'timeout': '超时', 'stalled': '长时间没有新动作（疑似卡死）',
          'looping': '原地打转', 'runaway': '命令数失控'}

PROTOCOL = """# 执行协议（派单方：Claude 指挥）

你是执行者。按下面的任务单完成工作，完成后会有独立评委评审，返工意见会发回本会话。

硬性边界：
1. 只在任务单写明的范围内改动；标「只读」的文件不得修改，需要改就另存新版本。
2. 禁止：git commit / push、部署上线、公开发布、删除文件、上传到任何云端、给任何人发消息、付费操作。需要这些就把 needs_user_action 设为 true 并在 summary 里写明要做什么。
3. 不读取、不外传任务单范围之外的私人资料（聊天记录、密码、密钥、证件、财务数据等）。
4. 不要修改用于验收的测试或检查脚本来让验收通过；验收不过就修产出本身。
5. 任务单没写清、会影响结果的取舍，选最保守的做法并写进 open_questions，不要自行扩大范围。
6. 完成前自己跑一遍验收方法，检查项和真实结果写进 verification；没跑过的不能写 passed=true。
7. changed_files 必须如实列出你新增、修改的全部文件（相对工作目录）。
8. 最终回复会被指挥逐字阅读：summary ≤150 字，open_questions 每条 ≤60 字，不贴代码、不复述任务单。

最终回复只输出符合 schema 的 JSON。status：success=全部完成且自检通过；partial=部分完成；failed=做不下去；blocked=必须用户本人操作才能继续（如登录）。
"""

NET_RULES = """
联网附加规则（本单已开联网）：
9. 只访问任务单需要的网址；只读取、下载、查询，不提交表单、不发帖评论、不发消息、不下单付费、不改任何线上数据。
10. 下载内容放进工作目录；引用网页信息时记下网址，写进 summary 或交付物。
"""

BROWSER_RULES = NET_RULES + """
浏览器附加规则（本单已放开沙箱以便启动 Chrome，你能访问整台电脑，更要守边界）：
11. 用 Playwright 启动本机 Chrome（channel='chrome'，默认 headless）；需要登录态时用任务单指定的持久化用户目录（launch_persistent_context），不要读取或复制日常 Chrome 的配置、Cookie、密码。
12. 只读写任务单列出的路径；不翻看其他目录。
13. 遇到登录页、验证码、扫码、短信，不要尝试绕过：改用 headless=False 打开同一用户目录的登录页留给用户本人完成，status 设为 blocked，summary 写明“请用户在弹出的浏览器里登录 X”。
14. 只做浏览、查询、截图、导出下载；任何点击会产生对外效果的按钮（提交、发布、发送、确认支付、删除）一律不点，写进 open_questions。
15. 截图、导出文件放工作目录，文件名写进 changed_files。
"""

SCHEMA = {
    'type': 'object',
    'properties': {
        'status': {'type': 'string', 'enum': ['success', 'partial', 'failed', 'blocked']},
        'summary': {'type': 'string'},
        'changed_files': {'type': 'array', 'items': {'type': 'string'}},
        'verification': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {'check': {'type': 'string'}, 'result': {'type': 'string'},
                           'passed': {'type': 'boolean'}},
            'required': ['check', 'result', 'passed'], 'additionalProperties': False}},
        'open_questions': {'type': 'array', 'items': {'type': 'string'}},
        'needs_user_action': {'type': 'boolean'},
    },
    'required': ['status', 'summary', 'changed_files', 'verification', 'open_questions',
                 'needs_user_action'],
    'additionalProperties': False,
}

FRIENDLY = [
    (r'usage limit|rate limit|quota|exceeded your|\b429\b',
     'Codex 额度用完或被限流。可选：①等额度恢复后续上 ②换轻快档重派 ③这单由指挥自己做'),
    (r'not logged in|unauthorized|\b401\b|please log ?in|login required',
     'Codex 登录失效：请用户在终端运行 codex login，或打开 ChatGPT 桌面版重新登录一次'),
    (r'model.{0,40}(not found|does not exist|unsupported|not available)',
     '模型不可用：跑 cxp models 后换档重派'),
    (r'ENOTFOUND|getaddrinfo|network is unreachable|connection (refused|reset)|stream disconnected',
     '网络问题：网络恢复后续上'),
]
HARMLESS = re.compile(r'models?[_ ]?(cache|list|refresh)|telemetry|update check', re.I)


# ---------- 通用 ----------

def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def die(msg, code=2):
    print('❌ ' + msg)
    sys.exit(code)


def load(p):
    return json.loads(Path(p).read_text())


def save(p, data):
    p = Path(p)
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    os.replace(tmp, p)


def user_default(key):
    if not CONFIG.exists():
        return None
    for line in CONFIG.read_text().splitlines():
        if line.startswith('['):
            break
        m = re.match(r'\s*%s\s*=\s*"([^"]+)"' % re.escape(key), line)
        if m:
            return m.group(1)
    return None


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def mac_notify(title, body):
    """系统通知，目前只支持 macOS；其他系统静默跳过。"""
    if os.environ.get('CODEX_DISPATCH_NO_NOTIFY') or sys.platform != 'darwin':
        return
    esc = lambda s: str(s).replace('\\', '\\\\').replace('"', '\\"')[:180]
    subprocess.run(['osascript', '-e', f'display notification "{esc(body)}" with title "{esc(title)}"'],
                   capture_output=True)


def task_dir(tid):
    d = ROOT / tid
    if not (d / 'meta.json').exists():
        die('找不到任务 ' + tid)
    return d


def all_tasks():
    if not ROOT.exists():
        return []
    return sorted(x for x in ROOT.iterdir() if (x / 'meta.json').exists())


def deny_list():
    """禁派清单：~/.config/codex-pipeline/deny.txt，每行一个路径片段，# 开头是注释。"""
    try:
        lines = DENY_FILE.read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith('#')]


def deny_rules():
    d = deny_list()
    return ('\n禁读路径：路径中含以下任一片段的文件，一律不读取、不外传：' + '、'.join(d) + '\n') if d else ''


def guard_privacy(*paths):
    for p in paths:
        full = str(Path(p).resolve()) + '/'
        for frag in deny_list():
            if frag.rstrip('/') in full:
                die(f'这个路径在禁派清单里（{frag}），不能交给 Codex（内容会发到云端）：{p}')


# ---------- 模型档位：随 Codex 官方清单自动更新 ----------

def load_tiers():
    return load(USER_TIERS if USER_TIERS.exists() else TIERS_FILE)


def inside(a, b):
    """路径 a 是否就是 b 或在 b 里面（按路径段比较，/a/foobar 不算在 /a/foo 里）。"""
    a, b = str(a).rstrip('/'), str(b).rstrip('/')
    return a == b or a.startswith(b + '/')


def model_list():
    try:
        return json.loads(MODELS_CACHE.read_text())['models']
    except Exception:
        return None


def version_of(slug):
    m = re.search(r'(\d+)(?:\.(\d+))?', slug)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)


def auto_pick(match):
    cands = [m for m in (model_list() or [])
             if m.get('visibility', 'list') == 'list'
             and match in (m.get('description') or '').lower()
             and not re.search(r'\b(previous|older|legacy|deprecated)\b', (m.get('description') or '').lower())]
    return max(cands, key=lambda m: version_of(m['slug']))['slug'] if cands else None


def resolve_tier(tier):
    tiers = load_tiers()
    t = tiers[tier]
    picked = auto_pick(t['match']) if tiers.get('auto', True) else None
    if picked:
        return picked, t['effort'], ''
    ms = model_list()
    if ms is None or t['pinned'] in {m['slug'] for m in ms}:
        return t['pinned'], t['effort'], ''
    fb = user_default('model')
    if not fb:
        die(f'{t["label"]}档没有可用的模型：先跑 cxp models 看清单，再用 --model 指定型号，'
            f'或在 {USER_TIERS} 里改 pinned')
    return fb, t['effort'], f'⚠️ {t["label"]}档没匹配到可用模型，已改用默认 {fb}'


def tier_label(tier):
    try:
        return load_tiers()[tier]['label']
    except Exception:
        return tier or '自定义'


def new_models_notice():
    known = set(load_tiers().get('known_models', []))
    new = [m for m in (model_list() or []) if m.get('visibility', 'list') == 'list' and m['slug'] not in known]
    if not new:
        return ''
    return ('🆕 Codex 有新模型：' + '；'.join(f"{m['slug']}（{(m.get('description') or '')[:30]}）" for m in new)
            + '。档位已自动匹配，收尾跑 cxp models --sync 并告诉用户')


# ---------- 改动对账 ----------

def snapshot(roots):
    files = {}
    for root in roots:
        for dp, dns, fns in os.walk(root):
            dns[:] = [x for x in dns if x not in SKIP_DIRS]
            for fn in fns:
                if fn == '.DS_Store' or fn.endswith(('.pyc', '.icloud')):
                    continue
                p = os.path.join(dp, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                files[p] = [st.st_size, st.st_mtime_ns]
                if len(files) > SNAP_LIMIT:
                    return None
    return files


def diff_snap(before, after):
    if before is None or after is None:
        return None
    added = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    modified = sorted(p for p in set(before) & set(after) if before[p] != after[p])
    return {'added': added, 'modified': modified, 'deleted': deleted}


def git_root(path):
    p = subprocess.run(['git', '-C', str(path), 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def make_baseline(d, meta, snap):
    """非 git 目录：派单前存一份文本文件副本，用来算累计 diff。"""
    if meta.get('git') or snap is None:
        return
    base = d / 'baseline'
    total = 0
    for p, (size, _) in snap.items():
        if Path(p).suffix.lower() not in TEXT_EXT or size > 1_000_000:
            continue
        total += size
        if total > BASELINE_MB * 1_000_000:
            meta['baseline_partial'] = True
            break
        dst = base / p.lstrip('/')
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(p, dst)
        except OSError:
            pass


def file_hash(p):
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    except OSError:
        return None


def protected_files(meta):
    out = []
    for pat in meta.get('protect', []):
        for p in Path(meta['cwd']).glob(pat):
            if p.is_file():
                out.append(str(p))
    return out


def guard_check(meta, changes):
    """越界 / 删除 / 改受保护文件 → 返回问题清单（删除与受保护属于硬失败）。"""
    hard, soft = [], []
    if changes:
        for p in changes['deleted']:
            hard.append('删除了文件：' + rel(meta, p))
        allow = meta.get('allow') or []
        if allow:
            for p in changes['added'] + changes['modified']:
                r = rel(meta, p)
                if not any(fnmatch.fnmatch(r, a) for a in allow):
                    soft.append('超出可改范围：' + r)
    for p, h in (meta.get('protect_hash') or {}).items():
        if file_hash(p) != h:
            hard.append('改动了受保护的验收文件：' + rel(meta, p))
    return hard, soft


def rel(meta, p):
    try:
        return str(Path(p).relative_to(meta['cwd']))
    except ValueError:
        return str(p)


# ---------- 监工 ----------

def kill_tree(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except Exception:
        pass
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def supervise(proc, events, progress_path, meta, rnd, started, timeout):
    mon = {'usage': None, 'commands': 0, 'files': set(), 'alarm_detail': '', 'errors': []}
    pos, buf = 0, ''
    last_activity = last_tick = time.time()
    running_cmd = last_msg = ''
    same_cmd, same_n = None, 0
    code = None
    while code is None:
        try:
            code = proc.wait(timeout=POLL)
        except subprocess.TimeoutExpired:
            pass
        tick = time.time()
        if tick - last_tick > POLL * 3 + 10:   # 合盖睡眠：这段空白不算卡死
            last_activity = tick
        last_tick = tick
        with open(events, errors='replace') as f:
            f.seek(pos)
            chunk = f.read()
            pos = f.tell()
        if chunk:
            last_activity = tick
            buf += chunk
            *lines, buf = buf.split('\n')
            for line in lines:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not meta.get('thread_id') and ev.get('thread_id'):
                    meta['thread_id'] = ev['thread_id']
                if ev.get('usage'):
                    mon['usage'] = ev['usage']
                if ev.get('type') in ('error', 'turn.failed'):
                    mon['errors'].append(json.dumps(ev, ensure_ascii=False)[:300])
                item = ev.get('item') or {}
                kind = item.get('type')
                if kind == 'command_execution':
                    c = item.get('command', '')
                    if ev.get('type') == 'item.started':
                        running_cmd = c
                        mon['commands'] += 1
                        same_n = same_n + 1 if c == same_cmd else 1
                        same_cmd = c
                    else:
                        running_cmd = ''
                elif kind == 'file_change':
                    same_n = 0   # 有改动说明在推进，不算打转
                    for ch in item.get('changes', []):
                        mon['files'].add(ch.get('path', ''))
                elif kind == 'agent_message':
                    last_msg = item.get('text', '')
        if code is not None:
            break
        elapsed, idle = tick - started, tick - last_activity
        stall_limit = meta['stall'] * (2 if running_cmd else 1)
        save(progress_path, {'task': meta['id'], 'round': rnd, 'updated_at': now(), 'pid': proc.pid,
                             'elapsed_s': round(elapsed), 'idle_s': round(idle),
                             'commands': mon['commands'], 'files_changed': len(mon['files']),
                             'running_command': running_cmd[:160], 'last_message': last_msg[:200]})
        if elapsed > timeout:
            code = 'timeout'
        elif idle > stall_limit:
            code = 'stalled'
            mon['alarm_detail'] = f'{round(idle)} 秒无新动作' + (f'，卡在：{running_cmd[:80]}' if running_cmd else '')
        elif same_n >= LOOP_REPEAT:
            code = 'looping'
            mon['alarm_detail'] = f'同一命令连续 {same_n} 次且没有任何改动：{same_cmd[:80]}'
        elif mon['commands'] > meta['max_commands']:
            code = 'runaway'
            mon['alarm_detail'] = f'已执行 {mon["commands"]} 条命令'
        if code is not None:
            kill_tree(proc)
    save(progress_path, {'task': meta['id'], 'round': rnd, 'updated_at': now(), 'finished': True,
                         'exit': code, 'commands': mon['commands'], 'files_changed': len(mon['files']),
                         'last_message': last_msg[:200]})
    return code, mon


def friendly_reason(stderr_text, errors):
    lines = [l for l in (stderr_text.splitlines() + errors) if l.strip() and not HARMLESS.search(l)]
    blob = '\n'.join(lines[-40:])
    for pat, msg in FRIENDLY:
        if re.search(pat, blob, re.I):
            return msg
    return None


# ---------- 执行一轮 ----------

def invoke(d, meta, rnd, prompt, resume, deadline):
    events, result, stderr = d / f'R{rnd}-events.jsonl', d / f'R{rnd}-result.json', d / f'R{rnd}-stderr.log'
    if meta.get('browser'):      # 浏览器单：macOS 沙箱拦截 Chrome 启动，只能放开
        sandbox = 'danger-full-access'
    elif meta.get('net'):        # 联网单：工作区可写 + 允许联网
        sandbox = 'workspace-write'
    else:
        sandbox = 'read-only' if meta.get('readonly') else 'workspace-write'
    net_opt = ['-c', 'sandbox_workspace_write.network_access=true'] if meta.get('net') and sandbox == 'workspace-write' else []
    common = ['--ignore-user-config', '--skip-git-repo-check', '--json', '-m', meta['model']] + net_opt + [
              '-c', f'model_reasoning_effort="{meta["effort"]}"',
              '-c', 'approval_policy="never"', '-c', f'sandbox_mode="{sandbox}"',
              '-c', 'sandbox_workspace_write.writable_roots=' + json.dumps(meta['add_dirs'], ensure_ascii=False),
              '--output-schema', str(ROOT / 'result-schema.json'), '-o', str(result)]
    # exec resume 不支持 --color / -C / -s
    cmd = ([CODEX, 'exec', 'resume'] + common + [meta['thread_id'], '-']) if resume else \
          ([CODEX, 'exec'] + common + ['--color', 'never', '-C', meta['cwd'], '-'])

    roots = [meta['cwd']] + meta['add_dirs']
    before = snapshot(roots)
    started = time.time()
    timeout = max(60, min(meta['timeout'], deadline - started))
    meta['rounds'].append({'round': rnd, 'kind': 'fix' if resume else 'run', 'tier': meta.get('tier'),
                           'model': meta['model'], 'effort': meta['effort'],
                           'started_at': now(), 'state': 'running', 'pid': None})
    meta['state'] = 'running'
    save(d / 'meta.json', meta)

    with open(events, 'w') as out, open(stderr, 'w') as err:
        proc = subprocess.Popen(cmd, cwd=meta['cwd'], stdin=subprocess.PIPE, stdout=out, stderr=err,
                                start_new_session=True, text=True)
        meta['rounds'][-1]['pid'] = proc.pid
        save(d / 'meta.json', meta)
        proc.stdin.write(prompt)
        proc.stdin.close()
        code, mon = supervise(proc, events, d / 'progress.json', meta, rnd, started, timeout)

    after = snapshot(roots)
    changes = diff_snap(before, after)
    r = meta['rounds'][-1]
    r.update({'finished_at': now(), 'seconds': round(time.time() - started), 'exit': code,
              'usage': mon['usage'], 'commands': mon['commands']})
    if changes is not None:
        r['changes'] = {k: [rel(meta, p) for p in v] for k, v in changes.items()}
    if code in ALARMS:
        r['alarm'] = ALARMS[code] + (('：' + mon['alarm_detail']) if mon.get('alarm_detail') else '')
    elif code == -signal.SIGTERM:
        r['alarm'] = '被叫停'

    payload = None
    if result.exists() and result.stat().st_mtime >= started:
        try:
            payload = json.loads(result.read_text())
        except ValueError:
            pass
    reason = None
    if code == 0 and payload:
        r['state'], r['status'] = 'done', payload['status']
        meta['state'] = 'awaiting_review'
        if payload.get('needs_user_action') and payload['status'] != 'blocked':
            meta.setdefault('pending', []).append({'round': rnd, 'what': payload['summary'][:150]})
    else:
        r['state'] = meta['state'] = 'error'
        if 'alarm' not in r:
            reason = friendly_reason(stderr.read_text(errors='replace'), mon['errors']) or 'Codex 异常退出'
            r['reason'] = reason

    verify = None
    if r['state'] == 'done':
        hard, soft = guard_check(meta, changes)
        verify = run_verify(meta)
        if hard:
            verify = verify or {'cmd': '（改动守则）', 'passed': True, 'tail': ''}
            verify['passed'] = False
            verify['tail'] = '\n'.join(hard) + ('\n' + verify['tail'] if verify['tail'] else '')
        r['verify'], r['guard_soft'] = verify, soft
    save(d / 'meta.json', meta)
    pack = write_pack(d, meta, rnd, payload, verify, changes, mon, stderr)
    return {'r': r, 'payload': payload, 'verify': verify, 'pack': pack, 'reason': reason}


def run_verify(meta):
    if not meta.get('verify'):
        return None
    try:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')   # 验收不在工作目录留下 __pycache__
        p = subprocess.run(meta['verify'], shell=True, cwd=meta['cwd'], capture_output=True, text=True,
                           timeout=900, env=env)
        out, ok = p.stdout + p.stderr, p.returncode == 0
    except subprocess.TimeoutExpired:
        out, ok = '验收命令超时（900 秒）', False
    return {'cmd': meta['verify'], 'passed': ok, 'tail': '\n'.join(out.strip().splitlines()[-25:])}


def write_pack(d, meta, rnd, payload, verify, changes, mon, stderr):
    """评审包：有行数上限；包含本轮改动、相对派单前的累计 diff、对账与验收结果。评委只读这一份。"""
    L = [f'# 评审包 R{rnd}｜{meta["title"]}', f'任务单：{d / "task.md"}', '']
    if payload:
        L += ['## Codex 自报', f'- 状态：{payload["status"]}', f'- 摘要：{payload["summary"]}']
        L += [f'- 自检 {"✓" if v["passed"] else "✗"} {v["check"]} → {v["result"]}' for v in payload.get('verification', [])]
        L += [f'- ❓ {q}' for q in payload.get('open_questions', [])]
    else:
        L += ['## 没有合规结果', '```', *stderr.read_text(errors='replace').strip().splitlines()[-20:], '```']
    if verify:
        L += ['', f'## 机器验收（脚本独立执行）：{"✅ 通过" if verify["passed"] else "❌ 未通过"}',
              f'`{verify["cmd"]}`', '```', verify['tail'], '```']
    L += ['', '## 本轮改动（脚本对账，不依赖 Codex 自报）']
    if changes is None:
        L.append('- 工作目录过大，未做全量对账；以下按 Codex 事件流列出：')
        L += [f'- {rel(meta, p)}' for p in sorted(mon['files']) if p]
    else:
        for k, cn in (('added', '新增'), ('modified', '修改'), ('deleted', '删除')):
            L += [f'- {cn}：{rel(meta, p)}' for p in changes[k]]
        if payload:
            detected = {rel(meta, p) for p in changes['added'] + changes['modified']}
            claimed = set(payload.get('changed_files', []))
            undeclared = sorted(detected - claimed)
            if undeclared:
                L.append('- ⚠️ 未申报的改动：' + '、'.join(undeclared[:20]))
    soft = (meta['rounds'][-1].get('guard_soft') or [])
    L += [f'- ⚠️ {s}' for s in soft[:20]]
    L += ['', '## 累计 diff（相对派单前，含历轮返工）']
    L += cumulative_diff(d, meta)
    out = d / f'R{rnd}-评审包.md'
    out.write_text('\n'.join(L) + '\n')
    return out


def cumulative_diff(d, meta):
    budget, L = DIFF_LINES, []
    touched = set()
    for r in meta['rounds']:
        for k in ('added', 'modified'):
            touched.update((r.get('changes') or {}).get(k, []))
    for f in sorted(touched):
        p = Path(meta['cwd']) / f
        if meta.get('git'):
            diff = subprocess.run(['git', '-C', meta['cwd'], 'diff', 'HEAD', '--', f],
                                  capture_output=True, text=True).stdout.splitlines()
            if not diff and p.exists():
                diff = [f'（新文件，{len(p.read_text(errors="replace").splitlines())} 行）']
        else:
            base = d / 'baseline' / str(p).lstrip('/')
            if p.suffix.lower() not in TEXT_EXT:
                diff = ['（非文本文件，评委需直接查看或看渲染图）']
            elif base.exists() and p.exists():
                diff = list(difflib.unified_diff(base.read_text(errors='replace').splitlines(),
                                                 p.read_text(errors='replace').splitlines(),
                                                 'before', 'after', lineterm=''))
            elif p.exists():
                diff = [f'（新文件，{len(p.read_text(errors="replace").splitlines())} 行，需要时直接读）']
            else:
                diff = ['（已不存在）']
        L.append(f'### {f}')
        if budget > 0:
            L += ['```diff', *diff[:budget], '```']
            if len(diff) > budget:
                L.append(f'…截断，余 {len(diff) - budget} 行，需要时直接读文件')
            budget -= len(diff)
        else:
            L.append(f'（diff 额度已用完，{len(diff)} 行，需要时直接读文件）')
    return L or ['（无）']


# ---------- 换档与自动返工 ----------

def round_failed(r):
    return r.get('state') == 'done' and (
        (r.get('verify') and not r['verify']['passed']) or r.get('status') in ('failed', 'partial'))


def maybe_escalate(meta):
    """连续 2 轮失败 → 升一档（不超过 max_tier；用户或指挥锁档后不动）。"""
    if meta.get('tier_locked'):
        return ''
    done = [r for r in meta['rounds'] if r['state'] == 'done']
    if len(done) < 2 or not all(round_failed(r) for r in done[-2:]):
        return ''
    cur = meta.get('tier') or 'normal'
    cap = meta.get('max_tier') or 'hard'
    nxt = TIER_ORDER[min(TIER_ORDER.index(cur) + 1, TIER_ORDER.index(cap))]
    if nxt == cur:
        return ''
    model, effort, _ = resolve_tier(nxt)
    meta.update({'tier': nxt, 'model': model, 'effort': effort})
    return f'⬆️ 连续两轮未过，自动升档：{tier_label(cur)} → {tier_label(nxt)} {model}/{effort}'


def report_paths(d, rnd):
    return sorted(str(p) for p in d.glob(f'R{rnd}-评审-*.md'))


def fix_prompt(d, fb_text, prev_rnd):
    reps = report_paths(d, prev_rnd)
    tail = ''
    if reps:
        tail = '\n\n评委完整报告（请逐份读完，逐条处理其中的「必修」）：\n' + '\n'.join('- ' + p for p in reps)
    return ('# 返工单（来自指挥）\n\n在本会话已有工作基础上处理下面的意见。同意的改掉，不同意的在 open_questions '
            '写明理由；改完重新跑验收，changed_files 列出本轮改动，最终仍按 schema 输出 JSON。\n\n'
            + fb_text + tail)


def run_rounds(d, meta, first_prompt, resume):
    """执行一轮；若机器验收失败且还有自动返工额度，脚本自己续一轮（不唤醒指挥）。"""
    deadline = time.time() + BUDGET
    lines = []
    rnd = len(meta['rounds']) + 1
    res = invoke(d, meta, rnd, first_prompt, resume, deadline)
    while (res['verify'] and not res['verify']['passed'] and meta.get('auto_fix', 0) > 0
           and meta.get('thread_id') and deadline - time.time() > 600
           and sum(1 for r in meta['rounds'] if r['state'] == 'done') < MAX_DONE_ROUNDS):
        meta['auto_fix'] -= 1
        esc = maybe_escalate(meta)
        if esc:
            lines.append(esc)
        lines.append(f'🔧 R{rnd} 机器验收未过，已自动返工一轮（不打扰指挥）')
        fb = '机器验收未通过，请修复产出本身（不要改测试/检查脚本）：\n```\n' + res['verify']['tail'] + '\n```'
        rnd += 1
        res = invoke(d, meta, rnd, fix_prompt(d, fb, rnd - 1), True, deadline)
    return res, lines, rnd


def report(meta, res, rnd, extra):
    """回调输出：会原样进指挥上下文，所以只给判断所需的最少信息。"""
    r, payload, verify = res['r'], res['payload'], res['verify']
    nchg = sum(len(v) for v in r['changes'].values()) if 'changes' in r else '?'
    print(f'任务 {meta["id"]} R{rnd}｜{tier_label(meta.get("tier"))} {meta["model"]}｜{r["seconds"]}s｜'
          f'{r["commands"]}条命令｜改{nchg}个文件')
    for l in extra:
        print(l)
    if r.get('alarm'):
        print('🚨 ' + r['alarm'])
    if res['reason']:
        print('❌ ' + res['reason'])
    if payload:
        print(f'Codex 自报：{payload["status"]}｜{payload["summary"][:120]}')
        for q in payload.get('open_questions', [])[:3]:
            print('  ❓ ' + q[:80])
    if verify:
        print(f'机器验收：{"✅ 通过" if verify["passed"] else "❌ 未通过"}｜{verify["cmd"][:60]}')
        if not verify['passed']:
            print('\n'.join('  ' + l for l in verify['tail'].splitlines()[-8:]))
    if r.get('guard_soft'):
        print(f'⚠️ 越界改动 {len(r["guard_soft"])} 处（详见评审包）')
    if meta.get('pending'):
        print(f'📝 待用户拍板 {len(meta["pending"])} 项（收尾时 cxp pending 一并列出）')
    if not meta.get('readonly') and r['state'] == 'done' and (not verify or verify['passed']):
        print(f'👉 请派评委：过程目录 {ROOT / meta["id"]}，轮次 R{rnd}')
    else:
        print(f'评审包：{res["pack"]}')
    # 通知只在需要用户时弹
    if r.get('alarm') and r['alarm'] != '被叫停' or res['reason']:
        mac_notify(f'⚠️ 流水线异常：{meta["title"]}', r.get('alarm') or res['reason'])
    elif payload and payload['status'] == 'blocked':
        mac_notify(f'🙋 流水线需要你：{meta["title"]}', payload['summary'][:120])
    return 0 if r['state'] == 'done' else 1


# ---------- 子命令 ----------

def read_text_arg(value, what):
    if value == '-':
        text = sys.stdin.read()
        if not text.strip():
            die(what + '为空')
        return text, None
    p = Path(value).expanduser().resolve()
    if not p.is_file():
        die(f'{what}不存在：{p}')
    return p.read_text(), p


def preflight():
    if not Path(CODEX).exists():
        die('找不到 Codex：请先安装 Codex CLI（npm i -g @openai/codex）或 ChatGPT 桌面版；'
            '装在别处就用环境变量 CODEX_BIN 指定完整路径')
    s = subprocess.run([CODEX, 'login', 'status'], capture_output=True, text=True)
    if 'Logged in' not in (s.stdout + s.stderr):
        die('Codex 没有登录：请用户在终端运行 codex login，或打开 ChatGPT 桌面版登录一次')


def cmd_new(a):
    cwd = Path(a.cwd).expanduser().resolve()
    if not cwd.is_dir():
        die('工作目录不存在：' + str(cwd))
    task_text, task = read_text_arg(a.task, '任务单')
    add_dirs = [str(Path(x).expanduser().resolve()) for x in a.add_dir]
    guard_privacy(cwd, *add_dirs, *([task] if task else []))
    if not a.shared:   # 同一目录并行会互相覆盖
        for x in all_tasks():
            m = load(x / 'meta.json')
            if m['state'] == 'running' and pid_alive((m['rounds'] or [{}])[-1].get('pid')) and (
                    inside(cwd, m['cwd']) or inside(m['cwd'], cwd)):
                die(f'「{m["title"]}」正在同一目录干活，并行会互相覆盖。请用 git worktree 分开，或加 --shared')
    tier = a.tier or ('easy' if (a.readonly or a.net or a.browser) else 'normal')  # 侦察、联网、浏览器单默认轻快档
    if a.max_tier and TIER_ORDER.index(tier) > TIER_ORDER.index(a.max_tier):
        tier = a.max_tier
    model, effort, note = resolve_tier(tier)
    slug = re.sub(r'[^\w一-鿿-]+', '-', a.title).strip('-')[:30]
    tid = datetime.now().strftime('%m%d-%H%M%S-') + slug
    d = ROOT / tid
    d.mkdir(parents=True)
    (d / 'task.md').write_text(task_text)
    meta = {'id': tid, 'title': a.title, 'cwd': str(cwd), 'add_dirs': add_dirs, 'created_at': now(),
            'state': 'new', 'thread_id': None, 'rounds': [], 'pending': [],
            'tier': tier, 'max_tier': a.max_tier, 'tier_locked': bool(a.lock_tier), 'tier_note': note,
            'model': a.model or model, 'effort': a.effort or effort,
            'timeout': a.timeout, 'stall': a.stall, 'max_commands': a.max_commands,
            'verify': a.verify, 'readonly': a.readonly, 'allow': a.allow, 'protect': a.protect,
            'auto_fix': 0 if a.readonly else a.auto_fix, 'git': bool(git_root(cwd)),
            'net': bool(a.net or a.browser), 'browser': bool(a.browser)}
    snap = snapshot([str(cwd)] + add_dirs)
    if snap is None:
        meta['snap_note'] = '工作目录文件过多，未做全量对账（建议 --cwd 指向项目子目录）'
    meta['protect_hash'] = {p: file_hash(p) for p in protected_files(meta)}
    make_baseline(d, meta, snap)
    save(d / 'meta.json', meta)
    return tid


def cmd_go(a):
    preflight()
    tid = cmd_new(a)
    d = ROOT / tid
    meta = load(d / 'meta.json')
    print(f'📤 已派出：{tid}｜{tier_label(meta["tier"])} {meta["model"]}/{meta["effort"]}')
    for k in ('tier_note', 'snap_note'):
        if meta.get(k):
            print(meta[k])
    n = new_models_notice()
    if n:
        print(n)
    sys.stdout.flush()
    prompt = PROTOCOL + deny_rules() + (BROWSER_RULES if meta.get('browser') else NET_RULES if meta.get('net') else '') + \
        '\n---\n\n# 任务单\n\n' + (d / 'task.md').read_text()
    res, extra, rnd = run_rounds(d, meta, prompt, resume=False)
    sys.exit(report(meta, res, rnd, extra))


def cmd_fix(a):
    d = task_dir(a.task_id)
    meta = load(d / 'meta.json')
    last = (meta['rounds'] or [{}])[-1]
    if meta['state'] == 'running' and pid_alive(last.get('pid')):
        die('这单还在跑，等它结束再返工')
    if not meta.get('thread_id'):
        die('没有可续接的 Codex 会话，请重新派单')
    if sum(1 for r in meta['rounds'] if r['state'] == 'done') >= MAX_DONE_ROUNDS:
        die('已满 3 轮返工，按流程交用户裁决', 3)
    fb_text, _ = read_text_arg(a.feedback, '返工单')
    prev = len(meta['rounds'])
    (d / f'R{prev + 1}-feedback.md').write_text(fb_text)
    extra = []
    if a.verify:
        meta['verify'] = a.verify
    if a.tier:
        model, effort, _ = resolve_tier(a.tier)
        extra.append(f'🔀 换档：{tier_label(meta.get("tier"))} → {tier_label(a.tier)} {model}/{effort}')
        meta.update({'tier': a.tier, 'model': model, 'effort': effort, 'tier_locked': True})
    else:
        esc = maybe_escalate(meta)
        if esc:
            extra.append(esc)
    print(f'🔁 R{prev + 1} 返工已派出：{meta["id"]}｜{tier_label(meta.get("tier"))} {meta["model"]}')
    sys.stdout.flush()
    res, more, rnd = run_rounds(d, meta, fix_prompt(d, fb_text, prev), resume=True)
    sys.exit(report(meta, res, rnd, extra + more))


def brief(d):
    m = load(d / 'meta.json')
    last = (m['rounds'] or [{}])[-1]
    state = m['state']
    if state == 'running' and not pid_alive(last.get('pid')):
        state = 'interrupted'
    line = f"{m['title']}｜{STATE_CN.get(state, state)}｜第{len(m['rounds'])}轮｜{tier_label(m.get('tier'))}"
    pg = d / 'progress.json'
    if state == 'running' and pg.exists():
        p = load(pg)
        line += f"｜已跑{p.get('elapsed_s', 0) // 60}分｜{p.get('commands', 0)}条命令｜改{p.get('files_changed', 0)}个文件"
        if p.get('running_command'):
            line += '｜在跑：' + p['running_command'][:50]
        elif p.get('last_message'):
            line += '｜' + p['last_message'][:50]
    elif last:
        v = last.get('verify')
        if v:
            line += '｜验收' + ('✅' if v['passed'] else '❌')
        if last.get('alarm'):
            line += '｜🚨' + last['alarm']
        elif last.get('reason'):
            line += '｜❌' + last['reason']
    if m.get('pending'):
        line += f"｜待拍板{len(m['pending'])}项"
    return line + f"｜id:{m['id']}"


def cmd_status(a):
    if a.task_id and a.full:
        print(json.dumps(load(task_dir(a.task_id) / 'meta.json'), ensure_ascii=False, indent=2))
        return 0
    dirs = [task_dir(a.task_id)] if a.task_id else [
        x for x in all_tasks() if a.all or load(x / 'meta.json')['state'] != 'closed']
    if not dirs:
        print('流水线没有进行中的任务')
    for d in dirs:
        print(brief(d))
    return 0


def cmd_pending(_):
    n = 0
    for d in all_tasks():
        m = load(d / 'meta.json')
        if m['state'] == 'closed':
            continue
        for p in m.get('pending', []):
            n += 1
            print(f'{n}. 【{m["title"]}】{p["what"]}')
    if not n:
        print('没有待拍板的事项')
    return 0


def cmd_stop(a):
    dirs = [task_dir(a.task_id)] if a.task_id else all_tasks()
    n = 0
    for d in dirs:
        m = load(d / 'meta.json')
        pid = (m['rounds'] or [{}])[-1].get('pid')
        if m['state'] == 'running' and pid_alive(pid):
            os.killpg(pid, signal.SIGTERM)
            n += 1
            print('已叫停：' + brief(d))
    if not n:
        print('没有在跑的任务')
    return 0


def cmd_close(a):
    d = task_dir(a.task_id)
    meta = load(d / 'meta.json')
    meta.update({'state': 'closed', 'verdict': a.verdict, 'closed_at': now()})
    archived = ''
    if a.archive:   # 把最新一轮各维度评审合并留痕到产出目录（脚本做，不花 Claude 的 token）
        rnd = max((int(re.match(r'R(\d+)-评审', p.name).group(1)) for p in d.glob('R*-评审-*.md')), default=0)
        reps = report_paths(d, rnd) if rnd else []
        if reps:
            out = Path(a.archive).expanduser() / f'{meta["title"]}-评审-{datetime.now():%Y-%m-%d}.md'
            body = [f'# {meta["title"]} 评审汇总（流水线 R{rnd}）', '', f'结论：{a.verdict}', '']
            for p in reps:
                body += ['---', '', Path(p).read_text(errors='replace'), '']
            out.write_text('\n'.join(body))
            archived = f'｜评审已存：{out}'
    save(d / 'meta.json', meta)
    print(f'已结案：{meta["title"]}｜{a.verdict}{archived}')
    if not a.quiet:
        ok = a.verdict.startswith(('🟢', '✅'))
        mac_notify(f'{"✅ 流水线完工" if ok else "⚠️ 流水线需要你看一下"}：{meta["title"]}', a.verdict)
    return 0


def cmd_models(a):
    tiers = load_tiers()
    for k in ('hard', 'normal', 'easy'):
        model, effort, note = resolve_tier(k)
        print(f'{tiers[k]["label"]}（{k}）：{model} / {effort}' + (f'  {note}' if note else ''))
    print(new_models_notice() or '没有新模型')
    if a.sync:
        for k in ('hard', 'normal', 'easy'):
            tiers[k]['pinned'] = resolve_tier(k)[0]
        tiers['known_models'] = sorted(m['slug'] for m in (model_list() or []) if m.get('visibility', 'list') == 'list')
        tiers['synced_at'] = datetime.now().strftime('%Y-%m-%d')
        USER_TIERS.parent.mkdir(parents=True, exist_ok=True)
        save(USER_TIERS, tiers)
        print(f'✅ 已同步到 {USER_TIERS}')
    return 0


def cmd_check(_):
    preflight()
    v = subprocess.run([CODEX, '--version'], capture_output=True, text=True).stdout.strip()
    print(f'✅ {v}，已登录')
    return cmd_models(argparse.Namespace(sync=False))


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    save(ROOT / 'result-schema.json', SCHEMA)
    p = argparse.ArgumentParser(description='流水线派单器')
    sub = p.add_subparsers(dest='cmd', required=True)
    g = sub.add_parser('go', help='一步派单（推荐）')
    g.add_argument('--title', required=True)
    g.add_argument('--cwd', required=True)
    g.add_argument('--task', default='-', help="任务单路径；默认从标准输入读")
    g.add_argument('--tier', choices=TIER_ORDER, help='easy 轻快 / normal 主力 / hard 顶配')
    g.add_argument('--max-tier', choices=TIER_ORDER, help='档位上限（用户说"省着点"时用 normal）')
    g.add_argument('--lock-tier', action='store_true', help='锁档，不自动升')
    g.add_argument('--verify', help='验收命令，脚本在工作目录独立执行')
    g.add_argument('--allow', action='append', default=[], help='可改范围（相对工作目录的 glob，可多次）')
    g.add_argument('--protect', action='append', default=[], help='受保护文件 glob（如测试），被改即验收失败')
    g.add_argument('--readonly', action='store_true', help='侦察单：只读')
    g.add_argument('--net', action='store_true', help='联网单：允许访问网络（抓网页、下载、调接口），只读不提交')
    g.add_argument('--browser', action='store_true', help='浏览器单：放开沙箱启动 Chrome（含联网），用于需要登录态的网页查询、截图、导出')
    g.add_argument('--shared', action='store_true', help='允许与其他任务共用目录')
    g.add_argument('--auto-fix', type=int, default=1, help='机器验收失败时脚本自动返工的轮数')
    g.add_argument('--add-dir', action='append', default=[])
    g.add_argument('--model')
    g.add_argument('--effort')
    g.add_argument('--timeout', type=int, default=3600, help='单轮超时（秒）')
    g.add_argument('--stall', type=int, default=900)
    g.add_argument('--max-commands', type=int, default=300)
    f = sub.add_parser('fix', help='返工（续接同一会话，自动附评委报告）')
    f.add_argument('task_id')
    f.add_argument('--feedback', default='-')
    f.add_argument('--tier', choices=TIER_ORDER)
    f.add_argument('--verify', help='更新验收命令')
    s = sub.add_parser('status')
    s.add_argument('task_id', nargs='?')
    s.add_argument('--full', action='store_true')
    s.add_argument('--all', action='store_true')
    sub.add_parser('pending', help='列出所有待用户拍板的事项')
    st = sub.add_parser('stop')
    st.add_argument('task_id', nargs='?')
    c = sub.add_parser('close')
    c.add_argument('task_id')
    c.add_argument('--verdict', required=True)
    c.add_argument('--archive', help='把最新一轮评审合并存到这个目录')
    c.add_argument('--quiet', action='store_true')
    mo = sub.add_parser('models')
    mo.add_argument('--sync', action='store_true')
    sub.add_parser('check')
    a = p.parse_args()
    sys.exit({'go': cmd_go, 'fix': cmd_fix, 'status': cmd_status, 'pending': cmd_pending,
              'stop': cmd_stop, 'close': cmd_close, 'models': cmd_models, 'check': cmd_check}[a.cmd](a))


if __name__ == '__main__':
    main()
