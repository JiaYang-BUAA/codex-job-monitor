"""Configured Codex delivery transport and atomic state persistence."""
import json
import time
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
ROOT = SOURCE / 'runtime'
THREAD = ''
SETTINGS = {}

def configure(config):
    global ROOT, THREAD
    SETTINGS.clear()
    SETTINGS.update(config)
    ROOT = Path(config['runtime_dir'])
    THREAD = config['thread_id']

def save(path, obj):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)

def client():
    from desktop_cdp_transport import DesktopCdpClient
    return DesktopCdpClient(SETTINGS['cdp_url'], 30)

def target_probe_expression():
    from desktop_cdp_transport import queue_lookup_code
    return '''(async () => {
''' + queue_lookup_code() + '''
  const threadId = ''' + json.dumps(THREAD) + ''';
  const manager = findDesktopManager();
  const result = await manager.requestClient.sendRequest('thread/read',
      {threadId, includeTurns:false});
  if (result?.thread?.id !== threadId || !result.thread.status?.type)
    throw Error('Configured thread is unavailable or has an unsupported schema');
  const access = await desktopQueueAccess(threadId);
  const items = await access.read();
  if (!Array.isArray(items)) throw Error('Unsupported queue schema');
  return {ok:true, threadId, status:result.thread.status.type, queuedCount:items.length};
})()'''

def guarded_expression(prompt, event_id):
    from desktop_cdp_transport import build_enqueue_queued_follow_up_expression
    expression = build_enqueue_queued_follow_up_expression(THREAD, prompt, SETTINGS['cwd'], event_id, int(time.time()*1000))
    anchor = '  const result = await access.enqueue(payload.message);'
    # Use the same Desktop connection for the last live check and the enqueue.
    guard = '''
  try {
  const existing = await access.read();
  if (existing.length > 0) return {ok:true, deferred:true, reason:'target_queue_not_empty'};
  const manager = findDesktopManager();
  const read = await manager.requestClient.sendRequest('thread/read',
      {threadId:payload.threadId, includeTurns:false});
  const status = read?.thread?.status?.type;
  if (status !== 'idle') return {ok:true, deferred:true, reason:'target_not_idle', status:status ?? 'unknown'};
  } catch(error) {
    return {ok:true, deferred:true, reason:'target_state_unavailable', detail:String(error)};
  }
'''
    if expression.count(anchor) != 1:
        raise RuntimeError('Queue helper changed; refusing unguarded submission')
    return expression.replace(anchor, guard + anchor)
