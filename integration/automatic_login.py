"""One guarded automatic login, with no collection requests or automatic retries.

The caller must first stop collection, preserve the failed request, and close the
owned collection context. A successful result can be passed to session_keeper.
"""
import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = Path('/home/mz/.local/share/sd_crawler/jibei-login/module')
PRIVATE = Path('/home/mz/.config/jibei-login')
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(MODULE))


def read_json(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('symlink_not_permitted')
    return json.loads(path.read_text())


def write_once(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with path.open('x') as f:
        os.chmod(path, 0o600)
        json.dump(value, f, ensure_ascii=False, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())


def checked_closure(root, path, portal, failed_identity):
    root, path = Path(root).resolve(), Path(path)
    if (path.is_symlink() or not path.resolve().is_relative_to(root / 'state/task_anomalies')
            or path.name not in {'context-closure.json', 'context-close-before-relogin-proof.json'}):
        raise ValueError('owned_closed_context_proof_required')
    proof = read_json(path)
    entry = read_json(path.parent / 'entry.json')
    context = entry.get('context', {})
    if (entry.get('identity') != failed_identity or context.get('portalTargetId') != portal
            or context.get('targetId') != proof.get('oldTargetId')):
        raise ValueError('closure_must_bind_failed_identity_and_portal')
    isolated = (proof.get('status') == 'isolated' and proof.get('oldTargetAbsent') is True
                and proof.get('portalTargetId') == portal and proof.get('businessRequestsSent') == 0)
    prelogin = (proof.get('allOwnedPagesClosed') is True and proof.get('portalPreserved') is True
                and proof.get('businessRequestsSent') == 0)
    if not (isolated or prelogin):
        raise ValueError('owned_closed_context_proof_required')
    return {'path': str(path.resolve().relative_to(root)),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'oldTargetId': proof.get('oldTargetId')}


def checked_private(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError('private_configuration_required')


def checked_keeper_auth_probe(root, result_path, closure_path, portal):
    """Bind a real 401 to the closed 504 context and a replaced portal realm."""
    root = Path(root).resolve()
    result_path = Path(result_path)
    base = root / 'state/session_keepalive'
    if (result_path.is_symlink() or result_path.name != 'result.json'
            or not result_path.resolve().is_relative_to(base / 'attempts')):
        raise ValueError('owned_authentication_probe_required')
    attempt = result_path.parent
    intent, result = read_json(attempt / 'intent.json'), read_json(result_path)
    wire, issued = read_json(attempt / 'wire-receipt.json'), read_json(attempt / 'issued.json')
    control, block = read_json(base / 'control.json'), read_json(base / 'block.json')
    failed = (root / block['attempt']).resolve()
    if (not failed.is_relative_to(base / 'attempts')
            or Path(closure_path).resolve().parent != root / 'state/task_anomalies/keeper' / failed.name):
        raise ValueError('keeper_failure_closure_binding_required')
    entry = read_json(Path(closure_path).parent / 'entry.json')
    prior = read_json(failed / 'result.json')
    prior_intent, prior_outcome = read_json(failed / 'intent.json'), read_json(failed / 'outcome.json')
    prior_wire = read_json(failed / 'receipt.json')
    prior_body = (failed / 'response.body').read_bytes()
    fence = prior_outcome.get('fileGuard', {})
    proof = read_json(closure_path)
    closure = checked_closure(root, closure_path, portal, entry['identity'])
    issued_at = datetime.fromisoformat(issued['issuedAt'].replace('Z', '+00:00')).timestamp() * 1000
    if (control.get('enabled') is not False or block.get('reason') != 'keeper_failed_or_unknown_no_retry'
            or intent.get('kind') != 'probe'
            or intent.get('sessionId') != control.get('sessionId')
            or result.get('sessionId') != control.get('sessionId')
            or result.get('outcome') != 'failed' or result.get('authRequired') is not True
            or result.get('businessStatus') != 401 or result.get('businessDataQueries') != 0
            or wire.get('httpStatus') != 200 or wire.get('businessStatus') != 401
            or wire.get('authRequired') is not True
            or wire.get('endpoint') != '/px-common-authcenter/auth/v2/information'
            or wire.get('responseSha256') != result.get('responseSha256')
            or issued.get('endpoint') != wire['endpoint'] or issued.get('method') != 'POST'
            or prior.get('reason') != 'origin_http_504' or prior.get('authRequired') is not False
            or prior.get('outcome') != 'failed' or prior_intent.get('kind') != 'renewal'
            or prior_intent.get('sessionId') != control.get('sessionId')
            or prior_wire.get('response', {}).get('status') != 504
            or prior_wire.get('fileId') != prior_intent.get('fileId')
            or prior_wire.get('sha256') != hashlib.sha256(prior_body).hexdigest()
            or prior_wire.get('bytes') != len(prior_body)
            or fence.get('sealed') is not True or fence.get('identitySealed') is not True
            or fence.get('lateFileRequestsBlocked') is not True or fence.get('trackedFileXhrsOpen') != 0
            or fence.get('nativeTransportVerified') is not True
            or fence.get('fileId') != prior_intent.get('fileId')
            or fence.get('attemptId') != failed.name
            or entry.get('intentSha256') != hashlib.sha256((failed / 'intent.json').read_bytes()).hexdigest()
            or entry.get('resultSha256') != hashlib.sha256((failed / 'result.json').read_bytes()).hexdigest()
            or proof.get('oldProfileContextDestroyed') is not True
            or proof.get('portalTimeOrigin', 0) <= issued_at):
        raise ValueError('closed_keeper_401_proof_required')
    return {'identity': entry['identity'], 'claimIdentity': attempt.name,
            'portalTargetId': portal, 'closure': closure,
            'sha256': hashlib.sha256(result_path.read_bytes()).hexdigest()}


def make_claim(root, failure_raw, failed_identity):
    key = hashlib.sha256(failed_identity.encode()).hexdigest()
    failure_sha = hashlib.sha256(failure_raw).hexdigest()
    path = Path(root) / 'state/automatic_login/claims' / (key + '.json')
    write_once(path, {'failureSha256': failure_sha, 'failedIdentity': failed_identity,
                      'at': time.time(), 'automaticRetry': False})
    return failure_sha


async def login_once(root, evidence, closure):
    from power_login import login
    from playwright.async_api import async_playwright

    attempt = Path(root) / 'state/automatic_login' / ('attempt-' + uuid.uuid4().hex)
    attempt.mkdir(parents=True, mode=0o700)
    intent = {'at': time.time(), 'portalTargetId': evidence['portalTargetId'],
              'trigger': 'user_authorized_automatic_login', 'priorUnknownTransportClosed': True,
              'failedFilesRetried': 0, 'closure': closure, 'authFailureSha256': evidence['sha256'],
              'failedIdentity': evidence['identity']}
    write_once(attempt / 'intent.json', intent)
    progress = ['initializing']
    def report(stage):
        progress[0] = stage
        print(json.dumps({'stage': stage}), flush=True)

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp('http://127.0.0.1:9223', timeout=15000)
            if len(browser.contexts) != 1:
                raise ValueError('unexpected_browser_context_count')
            ctx = browser.contexts[0]
            matches, live_targets = [], set()
            for candidate in ctx.pages:
                cdp = await ctx.new_cdp_session(candidate)
                target = (await cdp.send('Target.getTargetInfo'))['targetInfo']['targetId']
                await cdp.detach()
                live_targets.add(target)
                if target == evidence['portalTargetId']:
                    matches.append(candidate)
            if len(matches) != 1:
                raise ValueError('protected_portal_not_unique')
            if closure['oldTargetId'] in live_targets:
                raise ValueError('failed_context_still_present')
            portal = matches[0]
            # A stale dashboard can remain visible after the site's 401 response.
            if portal.url.endswith('/#/dashboard'):
                await portal.locator('.right-menu-item.el-dropdown').hover(timeout=8000)
                logout = portal.locator('li.el-dropdown-menu__item button').filter(has_text='退出登录')
                await logout.wait_for(state='visible', timeout=6000)
                if await logout.count() != 1:
                    raise ValueError('logout_control_not_unique')
                report('logout_started')
                await logout.click(timeout=10000)
                report('logout_completed')
            result = await asyncio.wait_for(login(
                portal, 'jibei', credentials_file=str(PRIVATE / 'credentials.json'),
                feishu_config=str(PRIVATE / 'feishu_config.json'), wait=75, sms_timeout=150,
                progress=report), timeout=360)
            if result.stage != 'logged_in':
                raise ValueError('automatic_login_not_completed')
            value = {'at': time.time(), 'stage': result.stage, 'lastProgress': progress[0],
                     'portalTargetId': evidence['portalTargetId']}
            write_once(attempt / 'result.json', value)
            return attempt / 'result.json'
    except Exception as exc:
        write_once(attempt / 'result.json', {'at': time.time(), 'stage': 'failed',
                   'lastProgress': progress[0], 'errorType': type(exc).__name__})
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--closure-proof', required=True)
    parser.add_argument('--auth-probe-result')
    args = parser.parse_args()
    from adapters.queue import Queue
    from adapters.relogin import checked_authentication_hold

    with (ROOT / 'state/turn.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not (ROOT / 'state/adaptation-stop').exists():
            raise ValueError('explicit_collection_stop_required')
        q = Queue()
        try:
            if q.db.execute("SELECT 1 FROM work WHERE status IN ('leased','sent')").fetchone():
                raise ValueError('unsettled_collection_request')
            if q.db.execute("SELECT 1 FROM window_reads WHERE status='sent'").fetchone():
                raise ValueError('unsettled_window_request')
        finally:
            q.close()
        portal = read_json(ROOT / 'state/session_keepalive/native-contract.json')['portalTargetId']
        if args.auth_probe_result:
            if (ROOT / 'state/collection-auth-hold.json').exists():
                raise ValueError('collection_auth_hold_requires_own_review')
            evidence = checked_keeper_auth_probe(ROOT, args.auth_probe_result, args.closure_proof, portal)
            failure_raw = Path(args.auth_probe_result).read_bytes()
            closure = evidence['closure']
            claim_identity = evidence['claimIdentity']
        else:
            hold_path = ROOT / 'state/collection-auth-hold.json'
            failure_raw = hold_path.read_bytes()
            hold = json.loads(failure_raw)
            q = Queue()
            try:
                checked_authentication_hold(q, hold)
            finally:
                q.close()
            closure = checked_closure(ROOT, args.closure_proof, portal, hold['identity'])
            evidence = {'portalTargetId': portal, 'identity': hold['identity']}
            claim_identity = hold['identity']
        for config in (PRIVATE / 'credentials.json', PRIVATE / 'feishu_config.json'):
            checked_private(config)
        failure_sha = make_claim(ROOT, failure_raw, claim_identity)
        evidence['sha256'] = failure_sha
        result = asyncio.run(login_once(ROOT, evidence, closure))
        print(json.dumps({'stage': 'logged_in', 'result': str(result.relative_to(ROOT))}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'stage': 'failed', 'errorType': type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1)
