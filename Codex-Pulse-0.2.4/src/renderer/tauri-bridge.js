(() => {
  const tauri = window.__TAURI__;
  const currentWindow = tauri?.window?.getCurrentWindow?.();
  const stateListeners = new Set();
  const commandListeners = new Set();

  if (!tauri?.core?.invoke || !tauri?.event?.listen) {
    console.error('Tauri API is unavailable');
    return;
  }

  const stateReady = tauri.event.listen('codex-pulse://state', event => {
    for (const callback of stateListeners) callback(event.payload);
  });

  const reportError = error => {
    const payload = {
      action: 'error',
      message: String(error || '操作失败').replace(/^Error:\s*/u, ''),
    };
    for (const callback of commandListeners) callback(payload);
  };

  window.codexPulse = Object.freeze({
    startDragging() {
      if (!currentWindow?.startDragging) {
        reportError('窗口拖动 API 不可用');
        return;
      }
      void currentWindow.startDragging().catch(reportError);
    },
    send(action, details = {}) {
      const safeDetails = details && typeof details === 'object' && !Array.isArray(details)
        ? details
        : {};
      void stateReady
        .then(() => tauri.core.invoke('handle_action', {
          payload: { action, ...safeDetails },
        }))
        .catch(reportError);
    },
    onState(callback) {
      if (typeof callback !== 'function') return () => {};
      stateListeners.add(callback);
      return () => stateListeners.delete(callback);
    },
    onCommand(callback) {
      if (typeof callback !== 'function') return () => {};
      commandListeners.add(callback);
      return () => commandListeners.delete(callback);
    },
  });
})();
