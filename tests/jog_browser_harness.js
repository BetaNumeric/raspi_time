const results = [];
const assert = (condition, message) => { if (!condition) throw new Error(message); };
let requests = [];
let keepaliveRequests = [];
let timers = new Map();
let nextTimer = 0;
let currentState = { jog_generation: 0, jog_epoch: 'epoch-a' };
const setTimeout = (callback) => { const id = ++nextTimer; timers.set(id, callback); return id; };
const clearTimeout = (id) => timers.delete(id);
const apiPost = (url, payload) => new Promise((resolve, reject) => requests.push({ ...payload, resolve, reject }));
let currentSequenceId = null;
const currentSequence = null;
const renderStatus = () => {};
const syncFormFromState = () => {};
const syncSequenceMetadataFromState = () => {};
const renderSequenceReadout = () => {};
const renderPreview = () => {};
const loadSequence = async () => {};
// APPLY_STATE_SOURCE
const postAction = async (action, extra) => {
    const result = await apiPost('/api/action', { action, ...extra });
    if (result.state) await applyState(result.state);
    return result;
};
const fetch = (url, options) => {
    keepaliveRequests.push({ ...JSON.parse(options.body), keepalive: options.keepalive });
    return Promise.resolve({ ok: true, json: async () => ({ state: { jog_generation: currentState.jog_generation + 1, jog_epoch: currentState.jog_epoch } }) });
};
class Button {
    constructor() { this.handlers = new Map(); this.captured = new Set(); }
    addEventListener(type, callback) { if (!this.handlers.has(type)) this.handlers.set(type, []); this.handlers.get(type).push(callback); }
    setPointerCapture(id) { if (this.rejectCapture) throw new Error('capture unavailable'); this.captured.add(id); }
    hasPointerCapture(id) { return this.captured.has(id); }
    releasePointerCapture(id) { this.captured.delete(id); this.emit('lostpointercapture', id); }
    emit(type, pointerId = 1, button = 0) {
        for (const callback of this.handlers.get(type) || []) callback({ pointerId, button, preventDefault() {} });
    }
}
// JOG_SOURCE
async function flush() { for (let i = 0; i < 12; i++) await Promise.resolve(); }
async function reset() {
    if (activeJogHold) stopJogHold();
    for (const request of requests) request.resolve({ state: { jog_generation: currentState.jog_generation + 1, jog_epoch: currentState.jog_epoch } });
    await flush();
    requests = [];
    keepaliveRequests = [];
    timers.clear();
    activeJogHold = null;
    pendingJogStops = Promise.resolve();
    currentState = { jog_generation: 10, jog_epoch: 'epoch-a' };
}
function button(direction = 1) { const result = new Button(); bindJogButton(result, direction); return result; }
function actions(action) { return requests.filter((request) => request.action === action); }
function answer(request, generation = currentState.jog_generation, epoch = currentState.jog_epoch) { request.resolve({ state: { jog_generation: generation, jog_epoch: epoch } }); }
function tick() { const [id, callback] = timers.entries().next().value; timers.delete(id); callback(); }
async function run(name, test) {
    await reset();
    try { await test(); results.push({ name, passed: true }); }
    catch (error) { results.push({ name, passed: false, error: error.message }); }
}
(async () => {
    await run('held jog renews and release stops its own session', async () => {
        const b = button(); b.emit('pointerdown'); await flush();
        const start = actions('jog_start')[0];
        assert(start.generation === 10, 'start must carry current generation');
        assert(start.epoch === 'epoch-a', 'start must carry current server epoch');
        assert(start.direction === 1, 'direction must be sent');
        assert(b.hasPointerCapture(1), 'pointer must be captured');
        assert(timers.size === 0, 'do not renew before start acknowledgement');
        answer(start); await flush();
        assert(timers.size === 1, 'start acknowledgement schedules renewal');
        tick(); await flush();
        const heartbeat = actions('jog_heartbeat')[0];
        assert(heartbeat.session_id === start.session_id, 'heartbeat must use hold session');
        answer(heartbeat); await flush();
        assert(timers.size === 1, 'one renewal timer after heartbeat');
        const staleTimer = timers.values().next().value;
        b.emit('pointerup');
        assert(actions('jog_stop')[0].session_id === start.session_id, 'release must be scoped');
        assert(timers.size === 0 && !activeJogHold && !b.hasPointerCapture(1), 'release must fully clean up');
        staleTimer(); await flush();
        assert(actions('jog_heartbeat').length === 1, 'stale timer must not renew released hold');
    });
    await run('release before start reply remains stopped', async () => {
        const b = button(); b.emit('pointerdown'); await flush();
        const start = actions('jog_start')[0]; b.emit('pointerup');
        assert(actions('jog_stop').length === 1, 'release sends stop before reply');
        answer(start); await flush();
        assert(actions('jog_stop').length === 2, 'delayed reply must be followed by another scoped stop');
        assert(actions('jog_stop').every((request) => request.session_id === start.session_id), 'both releases must be scoped');
        assert(timers.size === 0 && !activeJogHold, 'delayed start must not rearm');
    });
    await run('lost start response still sends stop', async () => {
        const b = button(); b.emit('pointerdown'); await flush();
        actions('jog_start')[0].reject(new Error('lost response')); await flush();
        assert(actions('jog_stop').length === 1, 'failed start must still be stopped');
        assert(timers.size === 0 && !activeJogHold, 'failed start cleans up hold');
    });
    await run('heartbeat rejection ends hold', async () => {
        const b = button(); b.emit('pointerdown'); await flush(); answer(actions('jog_start')[0]); await flush();
        tick(); await flush(); actions('jog_heartbeat')[0].reject(new Error('expired')); await flush();
        assert(actions('jog_stop').length === 1, 'failed heartbeat sends stop');
        assert(timers.size === 0 && !activeJogHold, 'failed heartbeat does not reschedule');
    });
    await run('next hold waits for previous stop generation', async () => {
        const first = button(1), second = button(-1);
        first.emit('pointerdown', 1); await flush();
        const oldStart = actions('jog_start')[0]; answer(oldStart); await flush();
        tick(); await flush();
        const oldHeartbeat = actions('jog_heartbeat')[0];
        second.emit('pointerdown', 2); await flush();
        assert(actions('jog_start').length === 1, 'new start must wait for old stop acknowledgement');
        answer(actions('jog_stop')[0], 11); await flush();
        const newStart = actions('jog_start')[1];
        assert(newStart.generation === 11 && newStart.direction === -1, 'new start must use generation after release');
        assert(newStart.session_id !== oldStart.session_id, 'holds need unique sessions');
        answer(oldHeartbeat); await flush(); answer(newStart, 11); await flush();
        assert(timers.size === 1, 'old heartbeat completion must not add another timer');
        first.emit('pointerup', 1);
        assert(actions('jog_stop').length === 1, 'old pointer release must not stop new hold');
        second.emit('pointerup', 2);
        assert(actions('jog_stop')[1].session_id === newStart.session_id, 'new release uses new session');
    });
    for (const type of ['pointercancel', 'pointerleave', 'lostpointercapture']) {
        await run(type + ' releases hold', async () => {
            const b = button(); b.emit('pointerdown'); await flush(); answer(actions('jog_start')[0]); await flush();
            b.emit(type);
            assert(actions('jog_stop').length === 1 && timers.size === 0 && !activeJogHold, type + ' must stop and clear timer');
        });
    }
    for (const type of ['blur', 'pagehide', 'visibilitychange']) {
        await run(type + ' sends keepalive release', async () => {
            const b = button(); b.emit('pointerdown'); await flush(); answer(actions('jog_start')[0]); await flush();
            if (type === 'visibilitychange') {
                Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
                document.dispatchEvent(new Event(type));
                delete document.visibilityState;
            } else window.dispatchEvent(new Event(type));
            await flush();
            assert(keepaliveRequests.length === 1 && keepaliveRequests[0].keepalive, type + ' must use keepalive');
            assert(timers.size === 0 && !activeJogHold, type + ' must clear active hold');
        });
    }
    await run('capture failure and nonprimary pointer cannot start motion', async () => {
        const b = button(); b.emit('pointerdown', 1, 2); await flush();
        assert(requests.length === 0 && !activeJogHold, 'ignore nonprimary button');
        b.rejectCapture = true; b.emit('pointerdown'); await flush();
        assert(actions('jog_start').length === 0 && !activeJogHold, 'capture failure must not leave a hold');
    });
    await run('out-of-order state cannot downgrade the jog generation', async () => {
        await applyState({ jog_generation: 12, jog_epoch: 'epoch-a' });
        await applyState({ jog_generation: 11, jog_epoch: 'epoch-a' });
        assert(currentState.jog_generation === 12, 'older response must retain current generation');
    });
    await run('server restart accepts reset generation in a new epoch', async () => {
        await applyState({ jog_generation: 100, jog_epoch: 'epoch-a' });
        await applyState({ jog_generation: 0, jog_epoch: 'epoch-b' });
        assert(currentState.jog_generation === 0, 'new server epoch must reset generation');
        const b = button(); b.emit('pointerdown'); await flush();
        const start = actions('jog_start')[0];
        assert(start.generation === 0 && start.epoch === 'epoch-b', 'new hold must carry restarted server state');
        answer(start); await flush();
        assert(timers.size === 1, 'jogging resumes after server restart without reload');
    });
    await run('missing epoch fails closed', async () => {
        currentState = { jog_generation: 10 };
        const b = button(); b.emit('pointerdown'); await flush();
        assert(actions('jog_start').length === 0 && !activeJogHold, 'missing server epoch must not start jog');
    });
    await run('missing generation fails closed', async () => {
        currentState = {};
        const b = button(); b.emit('pointerdown'); await flush();
        assert(actions('jog_start').length === 0 && !activeJogHold, 'missing controller state must not start jog');
    });
    document.body.replaceChildren(document.createTextNode(JSON.stringify(results)));
})().catch((error) => { document.body.textContent = JSON.stringify([{ name: 'harness', passed: false, error: error.stack }]); });
