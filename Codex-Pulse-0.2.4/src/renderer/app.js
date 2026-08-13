(() => {
  const stateMeta = {
    active: { label: '进行中', mark: '∿' },
    completed_pending: { label: '刚完成', mark: '✓' },
    completed: { label: '已完成', mark: '·' },
    attention: { label: '等待处理', mark: '!' },
    failed: { label: '执行失败', mark: '×' },
  };

  const sessionGroups = [
    { id: 'running', label: '正在进行', states: new Set(['attention', 'active']) },
    { id: 'completed_pending', label: '刚刚完成', states: new Set(['completed_pending']) },
    { id: 'failed', label: '执行失败', states: new Set(['failed']) },
    { id: 'history', label: '历史记录', states: new Set(['completed']) },
  ];
  const statePriority = new Map([
    ['attention', 0],
    ['active', 1],
    ['completed_pending', 2],
    ['failed', 3],
    ['completed', 4],
  ]);
  const defaultDisplayLimits = {
    active: 4,
    completed_pending: 3,
    failed: 1,
  };
  const displayLimitPayloadKeys = {
    active: 'active',
    completed_pending: 'completedPending',
    failed: 'failed',
  };
  const displayLimitKeys = Object.keys(defaultDisplayLimits);
  const listChromeHeight = 77;
  const focusGroupChromeHeight = 3 * 22;

  const appState = {
    sessions: [],
    remoteSessions: [],
    remoteHosts: [],
    discoveredRemoteHosts: [],
    remoteErrors: {},
    remoteConfigError: null,
    filter: 'focus',
    query: '',
    error: null,
    remoteLoading: false,
    yoloEnabled: false,
    windowPinned: false,
    sessionTitleMode: 'prompt',
    displayLimits: { ...defaultDisplayLimits },
    titleLines: 1,
  };

  const elements = {
    dragRegion: document.querySelector('#drag-region'),
    subtitle: document.querySelector('#subtitle'),
    list: document.querySelector('#session-list'),
    empty: document.querySelector('#empty-state'),
    search: document.querySelector('#search-input'),
    refresh: document.querySelector('#refresh-button'),
    pinButton: document.querySelector('#pin-button'),
    minimizeButton: document.querySelector('#minimize-button'),
    menuButton: document.querySelector('#menu-button'),
    menu: document.querySelector('#menu'),
    yoloToggle: document.querySelector('#yolo-toggle'),
    yoloBadge: document.querySelector('#yolo-badge'),
    manageRemoteMenu: document.querySelector('#manage-remote-menu'),
    remoteModal: document.querySelector('#remote-modal'),
    remoteModalClose: document.querySelector('#remote-modal-close'),
    remoteForm: document.querySelector('#remote-form'),
    remoteHostInput: document.querySelector('#remote-host-input'),
    remoteFormError: document.querySelector('#remote-form-error'),
    remoteHostList: document.querySelector('#remote-host-list'),
    discoveredHostList: document.querySelector('#discovered-host-list'),
    reloadSSHHosts: document.querySelector('#reload-ssh-hosts'),
    renameModal: document.querySelector('#rename-modal'),
    renameModalClose: document.querySelector('#rename-modal-close'),
    renameForm: document.querySelector('#rename-form'),
    renameInput: document.querySelector('#rename-input'),
    renameCancel: document.querySelector('#rename-cancel'),
    displaySettingsMenu: document.querySelector('#display-settings-menu'),
    displaySettingsModal: document.querySelector('#display-settings-modal'),
    displaySettingsClose: document.querySelector('#display-settings-close'),
    displaySettingsForm: document.querySelector('#display-settings-form'),
    displaySettingsCancel: document.querySelector('#display-settings-cancel'),
    focusLimitTotal: document.querySelector('#focus-limit-total'),
    emptyAction: document.querySelector('#empty-action'),
    health: document.querySelector('#health-dot'),
    sessionContextMenu: document.querySelector('#session-context-menu'),
  };

  const bridge = (action, details = {}) => window.codexPulse?.send(action, details);

  const currentSessions = () => [...appState.sessions, ...appState.remoteSessions].sort((left, right) => {
    const priority = (statePriority.get(left.state) ?? 99) - (statePriority.get(right.state) ?? 99);
    return priority || Number(right.updatedAt) - Number(left.updatedAt);
  });
  const currentError = () => appState.error || appState.remoteConfigError || null;
  const currentIssues = () => {
    const issues = appState.error ? [{
        id: 'local-read-error',
        title: '本地会话读取失败',
        detail: appState.error,
        action: 'refresh',
      }] : [];
    return issues.concat(Object.entries(appState.remoteErrors).map(([host, detail]) => ({
        id: `remote-error:${host}`,
        title: `${host} 连接失败`,
        detail,
        action: 'remoteConnect',
        host,
      })));
  };
  const count = state => currentSessions().filter(session => session.state === state).length;
  const normalizeDisplayLimit = (value, fallback) => {
    const number = Number(value);
    return Number.isInteger(number) && number >= 1 && number <= 8 ? number : fallback;
  };
  const focusDisplayLimit = () => displayLimitKeys.reduce(
    (total, key) => total + appState.displayLimits[key],
    0,
  );
  const focusWindowHeight = () => {
    const rowHeight = appState.titleLines === 1 ? 43 : 58;
    return listChromeHeight + focusGroupChromeHeight + focusDisplayLimit() * rowHeight + 8;
  };
  let layoutFrame = null;
  let lastRequestedHeight = null;

  function scheduleWindowHeight() {
    if (layoutFrame !== null) cancelAnimationFrame(layoutFrame);
    layoutFrame = requestAnimationFrame(() => {
      layoutFrame = null;
      const listHeight = focusWindowHeight();
      let height = listHeight;
      if (!elements.remoteModal.hidden) {
        height = Math.max(listHeight, 440);
      } else if (!elements.displaySettingsModal.hidden) {
        height = Math.max(listHeight, 430);
      }
      const normalized = Math.max(190, Math.min(1600, Math.round(height)));
      if (normalized === lastRequestedHeight) return;
      lastRequestedHeight = normalized;
      bridge('setWindowHeight', { height: normalized });
    });
  }

  function sessionMatchesFilter(session, filter) {
    if (filter === 'focus') {
      return session.state === 'active'
        || session.state === 'attention'
        || session.state === 'completed_pending'
        || session.state === 'failed';
    }
    if (filter === 'active') return session.state === 'active' || session.state === 'attention';
    return filter === 'all' || session.state === filter;
  }

  function receive(payload) {
    appState.sessions = Array.isArray(payload.sessions) ? payload.sessions : [];
    appState.remoteSessions = Array.isArray(payload.remoteSessions) ? payload.remoteSessions : [];
    appState.remoteHosts = Array.isArray(payload.remoteHosts) ? payload.remoteHosts : [];
    appState.discoveredRemoteHosts = Array.isArray(payload.discoveredRemoteHosts) ? payload.discoveredRemoteHosts : [];
    appState.remoteErrors = payload.remoteErrors && typeof payload.remoteErrors === 'object' ? payload.remoteErrors : {};
    appState.remoteConfigError = typeof payload.remoteConfigError === 'string' ? payload.remoteConfigError : null;
    appState.error = typeof payload.error === 'string' ? payload.error : null;
    appState.remoteLoading = Boolean(payload.remoteLoading);
    if (typeof payload.yoloEnabled === 'boolean') appState.yoloEnabled = payload.yoloEnabled;
    if (typeof payload.windowPinned === 'boolean') appState.windowPinned = payload.windowPinned;
    if (payload.sessionTitleMode === 'prompt' || payload.sessionTitleMode === 'title') {
      appState.sessionTitleMode = payload.sessionTitleMode;
    }
    const receivedLimits = payload.displayLimits && typeof payload.displayLimits === 'object'
      ? payload.displayLimits
      : {};
    appState.displayLimits = Object.fromEntries(displayLimitKeys.map(key => [
      key,
      normalizeDisplayLimit(
        receivedLimits[key] ?? receivedLimits[displayLimitPayloadKeys[key]],
        defaultDisplayLimits[key],
      ),
    ]));
    appState.titleLines = payload.titleLines === 2 ? 2 : 1;
    elements.reloadSSHHosts.textContent = '重新读取';
    render();
  }

  function render() {
    document.body.classList.toggle('yolo-enabled', appState.yoloEnabled);
    document.body.classList.toggle('title-lines-1', appState.titleLines === 1);
    elements.yoloToggle.classList.toggle('enabled', appState.yoloEnabled);
    elements.yoloToggle.setAttribute('aria-checked', String(appState.yoloEnabled));
    elements.yoloBadge.hidden = !appState.yoloEnabled;
    elements.pinButton.classList.toggle('selected', appState.windowPinned);
    elements.pinButton.setAttribute('aria-pressed', String(appState.windowPinned));
    elements.pinButton.title = appState.windowPinned ? '取消固定' : '固定窗口';
    elements.pinButton.setAttribute('aria-label', elements.pinButton.title);
    document.querySelectorAll('[data-session-title-mode]').forEach(button => {
      const selected = button.dataset.sessionTitleMode === appState.sessionTitleMode;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
    elements.refresh.classList.toggle('spinning', appState.remoteLoading);
    renderRemoteManagement();

    const sessions = currentSessions();
    const issues = currentIssues();
    const completedPending = count('completed_pending');
    const running = count('active') + count('attention');
    const failures = count('failed') + issues.length;
    const focusCount = running + completedPending + failures;
    document.querySelector('#count-focus').textContent = String(focusCount);
    document.querySelector('#count-active').textContent = String(running);
    document.querySelector('#count-completed_pending').textContent = String(completedPending);
    document.querySelector('#count-failed').textContent = String(failures);
    document.querySelector('#count-all').textContent = String(sessions.length + issues.length);

    const error = currentError();
    const summary = [];
    if (running > 0) summary.push(`${running} 进行`);
    if (completedPending > 0) summary.push(`${completedPending} 新完成`);
    if (failures > 0) summary.push(`${failures} 失败`);
    elements.subtitle.textContent = summary.length > 0
      ? summary.join(' · ')
      : appState.remoteLoading && sessions.length === 0
        ? '正在读取本地和远程会话…'
        : '当前没有需要关注的会话';

    document.querySelectorAll('[data-filter]').forEach(button => {
      button.classList.toggle('selected', button.dataset.filter === appState.filter);
    });

    const query = appState.query.trim().toLocaleLowerCase('zh-CN');
    const visible = sessions.filter(session => {
      const stateMatches = query && appState.filter === 'focus'
        ? true
        : sessionMatchesFilter(session, appState.filter);
      const textMatches = !query || `${session.lastPrompt || ''} ${session.title} ${session.cwd} ${session.remoteHost || ''}`.toLocaleLowerCase('zh-CN').includes(query);
      return stateMatches && textMatches;
    });
    const visibleIssues = issues.filter(issue => {
      const filterMatches = appState.filter === 'focus'
        || appState.filter === 'failed'
        || appState.filter === 'all';
      const textMatches = !query
        || `${issue.title} ${issue.detail} ${issue.host || ''}`.toLocaleLowerCase('zh-CN').includes(query);
      return filterMatches && textMatches;
    });

    elements.list.replaceChildren(...createSessionList(visible, visibleIssues));
    renderEmptyState(visible.length + visibleIssues.length);
    scheduleWindowHeight();

    elements.health.classList.toggle('error', issues.length > 0);
    elements.health.classList.remove('remote');
    elements.health.classList.toggle('yolo', appState.yoloEnabled && issues.length === 0);
    elements.health.title = error
      || (issues.length > 0
        ? `${issues.length} 个读取或连接失败 · 其余会话已显示`
        : appState.remoteLoading
          ? '正在同步本地和远程会话…'
          : `${appState.sessions.length} 个本地会话 · ${appState.remoteSessions.length} 个远程会话`);
    elements.search.placeholder = '搜索';
  }

  function renderRemoteManagement() {
    elements.remoteFormError.hidden = !appState.remoteConfigError;
    elements.remoteFormError.textContent = appState.remoteConfigError || '';

    if (appState.remoteHosts.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'remote-host-empty';
      empty.textContent = '还没有配置远程服务器';
      elements.remoteHostList.replaceChildren(empty);
    } else {
      elements.remoteHostList.replaceChildren(...appState.remoteHosts.map(host => {
        const item = document.createElement('div');
        item.className = `remote-host-item${appState.remoteErrors[host] ? ' has-error' : ''}`;
        const light = document.createElement('span');
        light.className = 'host-light';
        const copy = document.createElement('span');
        copy.className = 'remote-host-copy';
        const name = document.createElement('strong');
        name.textContent = host;
        const detail = document.createElement('small');
        const sessionCount = appState.remoteSessions.filter(session => session.remoteHost === host).length;
        detail.textContent = appState.remoteErrors[host] || `${sessionCount} 个 Codex 会话`;
        copy.append(name, detail);
        const connect = document.createElement('button');
        connect.textContent = '连接';
        connect.addEventListener('click', () => bridge('remoteConnect', { host }));
        const remove = document.createElement('button');
        remove.className = 'remove';
        remove.textContent = '移除';
        remove.addEventListener('click', () => bridge('removeRemoteHost', { host }));
        item.append(light, copy, connect, remove);
        return item;
      }));
    }

    if (appState.discoveredRemoteHosts.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'remote-host-empty compact';
      empty.textContent = '没有发现具体的 Host 别名';
      elements.discoveredHostList.replaceChildren(empty);
      return;
    }
    elements.discoveredHostList.replaceChildren(...appState.discoveredRemoteHosts.map(host => {
      const alreadyAdded = appState.remoteHosts.includes(host);
      const item = document.createElement('div');
      item.className = 'remote-host-item discovered';
      const light = document.createElement('span');
      light.className = 'host-light';
      const copy = document.createElement('span');
      copy.className = 'remote-host-copy';
      const name = document.createElement('strong');
      name.textContent = host;
      const detail = document.createElement('small');
      detail.textContent = 'SSH config';
      copy.append(name, detail);
      const add = document.createElement('button');
      add.textContent = alreadyAdded ? '已添加' : '添加';
      add.disabled = alreadyAdded;
      add.addEventListener('click', () => bridge('addRemoteHost', { host }));
      item.append(light, copy, add);
      return item;
    }));
  }

  function createSessionList(sessions, issues) {
    const nodes = [];
    for (const group of sessionGroups) {
      const members = sessions.filter(session => group.states.has(session.state));
      const groupIssues = group.id === 'failed' ? issues : [];
      if (members.length === 0 && groupIssues.length === 0) continue;

      let displayedMembers = members;
      let displayedIssues = groupIssues;
      if (appState.filter === 'focus' && !appState.query.trim()) {
        const limitKey = group.id === 'running' ? 'active' : group.id;
        const limit = appState.displayLimits[limitKey];
        if (Number.isInteger(limit)) {
          displayedIssues = groupIssues.slice(0, limit);
          displayedMembers = members.slice(0, Math.max(0, limit - displayedIssues.length));
        }
      }

      const heading = document.createElement('div');
      heading.className = 'session-group-heading';
      heading.dataset.group = group.id;
      const label = document.createElement('strong');
      label.textContent = group.label;
      const countLabel = document.createElement('span');
      const total = members.length + groupIssues.length;
      const waiting = group.id === 'running'
        ? members.filter(session => session.state === 'attention').length
        : 0;
      const displayed = displayedMembers.length + displayedIssues.length;
      const hidden = Math.max(0, total - displayed);
      const focusGroup = appState.filter === 'focus' && !appState.query.trim() && group.id !== 'history';
      if (focusGroup) {
        const viewButton = document.createElement('button');
        viewButton.className = 'group-view-button';
        viewButton.type = 'button';
        viewButton.textContent = '查看';
        viewButton.setAttribute('aria-label', `查看全部${group.label}`);
        viewButton.addEventListener('click', () => {
          appState.filter = group.id === 'running' ? 'active' : group.id;
          render();
        });
        if (hidden > 0) {
          countLabel.append(`${displayed}/${total} · 隐藏 ${hidden} `, viewButton);
          countLabel.title = `共 ${total} 条，已隐藏 ${hidden} 条${waiting > 0 ? `，其中 ${waiting} 条等待处理` : ''}`;
        } else {
          countLabel.append(waiting > 0 ? `${total} · ${waiting} 等待 · ` : `${total} · `, viewButton);
        }
      } else {
        countLabel.textContent = waiting > 0 ? `${total} · ${waiting} 等待处理` : String(total);
      }
      heading.append(label, countLabel);
      nodes.push(
        heading,
        ...displayedIssues.map(createIssueRow),
        ...displayedMembers.map(createSessionRow),
      );
    }
    return nodes;
  }

  function createIssueRow(issue) {
    const row = document.createElement('article');
    row.className = 'system-issue';

    const mark = document.createElement('span');
    mark.className = 'issue-mark';
    mark.textContent = '!';

    const copy = document.createElement('div');
    copy.className = 'issue-copy';
    const title = document.createElement('strong');
    title.textContent = issue.title;
    const detail = document.createElement('span');
    detail.textContent = issue.detail;
    detail.title = issue.detail;
    copy.append(title, detail);

    const action = document.createElement('button');
    action.className = 'issue-action';
    const isRemote = issue.action === 'remoteConnect';
    action.title = isRemote ? '在终端连接服务器' : '重新读取会话';
    action.setAttribute('aria-label', action.title);
    action.innerHTML = isRemote
      ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4h14v16H5z"/><path d="m8 9 3 3-3 3m5 0h3"/></svg>'
      : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.34 5.66M20 11V5m0 6h-6"/></svg>';
    action.addEventListener('click', () => {
      if (isRemote) bridge('remoteConnect', { host: issue.host });
      else bridge('refresh');
    });

    row.append(mark, copy, action);
    return row;
  }

  function renderEmptyState(visibleCount) {
    elements.empty.hidden = visibleCount > 0;
    elements.empty.classList.remove('loading');
    if (visibleCount > 0) return;

    const icon = elements.empty.querySelector('.empty-icon');
    const title = elements.empty.querySelector('strong');
    const detail = elements.empty.querySelector('span');
    const error = currentError();
    const sessions = currentSessions();
    elements.emptyAction.hidden = true;
    elements.emptyAction.dataset.action = '';
    delete elements.emptyAction.dataset.host;
    if (appState.remoteLoading && sessions.length === 0) {
      icon.textContent = '⇄';
      title.textContent = '正在读取 Codex 会话';
      detail.textContent = '正在同步本地和已配置的远程服务器。';
    } else if (error) {
      icon.textContent = '!';
      title.textContent = '无法读取 Codex 会话';
      detail.textContent = error;
    } else if (sessions.length === 0) {
      icon.textContent = '>_';
      title.textContent = '还没有 Codex 会话';
      detail.textContent = '本地或服务器上的 Codex 会话会统一出现在这里。';
    } else if (!appState.query && appState.filter === 'focus') {
      icon.textContent = '✓';
      title.textContent = '没有需要关注的会话';
      detail.textContent = '当前没有进行中、刚完成或失败的任务。';
    } else if (!appState.query && appState.filter === 'active') {
      icon.textContent = '∿';
      title.textContent = '当前没有进行中的会话';
      detail.textContent = '等待任务启动后，这里会优先显示。';
    } else if (!appState.query && appState.filter === 'completed_pending') {
      icon.textContent = '✓';
      title.textContent = '没有刚完成的任务';
      detail.textContent = '新完成的任务会保留在这里，直到你确认。';
    } else if (!appState.query && appState.filter === 'failed') {
      icon.textContent = '✓';
      title.textContent = '没有执行失败';
      detail.textContent = '当前没有会话错误或连接故障。';
    } else {
      icon.textContent = '⌕';
      title.textContent = '没有匹配的会话';
      detail.textContent = '切换状态或修改搜索词查看其他内容。';
    }
  }

  function createSessionRow(session) {
    const meta = stateMeta[session.state] || stateMeta.completed;
    const row = document.createElement('article');
    row.className = 'session-row';
    row.dataset.state = session.state;
    row.addEventListener('contextmenu', event => {
      event.preventDefault();
      openSessionContextMenu(session, event.clientX, event.clientY);
    });

    const top = document.createElement('div');
    top.className = 'row-top';

    const mark = document.createElement('span');
    mark.className = session.state === 'active' ? 'state-mark spinner' : 'state-mark';
    if (session.state !== 'active') mark.textContent = meta.mark;

    const copy = document.createElement('div');
    copy.className = 'row-copy';
    const title = document.createElement('h2');
    title.className = 'row-title';
    const titleText = appState.sessionTitleMode === 'title'
      ? (session.title || session.lastPrompt)
      : (session.lastPrompt || session.title);
    title.textContent = titleText || '无标题会话';
    title.title = title.textContent;

    const detail = document.createElement('div');
    detail.className = 'row-meta';
    const stateLabel = document.createElement('span');
    stateLabel.className = 'state-label';
    stateLabel.textContent = meta.label;
    detail.append(stateLabel);

    const project = document.createElement('span');
    project.className = 'project';
    project.title = session.source === 'remote' ? `${session.remoteHost} · ${session.cwd}` : session.cwd;
    const projectLabel = session.source === 'remote'
      ? `${session.remoteHost} · ${session.projectName || '远程目录'}`
      : session.projectName;
    project.textContent = projectLabel || '';
    if (projectLabel) detail.append(separator(), project);
    if (session.writerOwner) {
      const writerOwner = document.createElement('span');
      writerOwner.className = 'writer-owner';
      writerOwner.textContent = session.writerOwner;
      writerOwner.title = `${session.writerOwner}${session.writerTty ? ` · ${session.writerTty}` : ''}${session.pid ? ` · PID ${session.pid}` : ''}`;
      detail.append(separator(), writerOwner);
    }
    if (session.model) {
      const model = document.createElement('span');
      model.className = 'model';
      model.textContent = session.model;
      detail.append(separator(), model);
    }
    if (session.state !== 'completed' && session.detail) {
      const detailText = document.createElement('span');
      detailText.className = 'detail-text';
      detailText.textContent = session.detail;
      detail.append(separator(), detailText);
    }
    copy.append(title, detail);

    const time = document.createElement('time');
    time.className = 'row-time';
    time.textContent = relativeTime(session.updatedAt);
    let rowTail = time;
    if (session.state === 'completed_pending' && session.completionKey) {
      const acknowledge = document.createElement('button');
      acknowledge.className = 'acknowledge-button';
      acknowledge.textContent = '已查看';
      acknowledge.addEventListener('click', event => {
        event.stopPropagation();
        acknowledge.disabled = true;
        bridge('acknowledgeCompletion', { id: session.id, completionKey: session.completionKey });
      });
      rowTail = acknowledge;
    }
    top.append(mark, copy, rowTail);

    const actions = document.createElement('span');
    actions.className = 'row-actions';
    const writerActive = Number.isInteger(session.pid) && session.pid > 0;
    const resumeBlocked = writerActive || session.state === 'active' || session.state === 'attention';
    const writerOwner = session.writerOwner || (session.source === 'remote' ? '远程终端' : '终端');
    if (session.source === 'remote') {
      actions.append(
        actionButton(
          'remoteCopy',
          resumeBlocked
            ? `会话已在${writerOwner}运行`
            : (appState.yoloEnabled ? '复制远程 YOLO 继续命令' : '复制远程继续命令'),
          '<rect x="8" y="8" width="11" height="11" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
          session.id,
          {},
          resumeBlocked,
        ),
        actionButton('remoteConnect', '在终端连接服务器', '<path d="M5 4h14v16H5z"/><path d="m8 9 3 3-3 3m5 0h3"/>', session.id, { host: session.remoteHost }),
        actionButton(
          'remoteResume',
          resumeBlocked
            ? `会话已在${writerOwner}运行`
            : (appState.yoloEnabled ? '通过 SSH 以 YOLO 模式继续' : '通过 SSH 继续会话'),
          '<path d="M8 7 4 12l4 5M16 7l4 5-4 5M10 19l4-14"/>',
          session.id,
          {},
          resumeBlocked,
        ),
      );
    } else {
      actions.append(
        actionButton(
          'copy',
          resumeBlocked
            ? `会话已在${writerOwner}运行`
            : (appState.yoloEnabled ? '复制 YOLO 继续命令' : '复制继续命令'),
          '<rect x="8" y="8" width="11" height="11" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
          session.id,
          {},
          resumeBlocked,
        ),
        actionButton('reveal', '在 Finder 中显示项目', '<path d="M3 7h7l2 2h9v10H3z"/><path d="M3 7V5h7l2 2"/>', session.id),
        actionButton(
          'resume',
          resumeBlocked
            ? `会话已在${writerOwner}运行`
            : (appState.yoloEnabled ? '以 YOLO 模式在终端中继续' : '在终端中继续'),
          '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3m6 0h4"/>',
          session.id,
          {},
          resumeBlocked,
        ),
      );
    }
    row.append(top, actions);
    return row;
  }

  function closeSessionContextMenu() {
    elements.sessionContextMenu.hidden = true;
    elements.sessionContextMenu.replaceChildren();
  }

  function openSessionContextMenu(session, clientX, clientY) {
    const remote = session.source === 'remote';
    const writerActive = Number.isInteger(session.pid) && session.pid > 0;
    const resumeBlocked = writerActive || session.state === 'active' || session.state === 'attention';
    const deletionBlocked = resumeBlocked;
    const owner = session.writerOwner || (remote ? '远程终端' : '终端');
    const items = [];
    const addAction = (label, action, options = {}) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = label;
      button.disabled = Boolean(options.disabled);
      button.classList.toggle('danger', Boolean(options.danger));
      if (!button.disabled) {
        button.addEventListener('click', () => {
          closeSessionContextMenu();
          action();
        });
      }
      items.push(button);
    };
    const addSeparator = () => {
      const separatorElement = document.createElement('div');
      separatorElement.className = 'session-context-separator';
      separatorElement.setAttribute('role', 'separator');
      items.push(separatorElement);
    };

    if (remote) {
      addAction(resumeBlocked ? `会话已在${owner}运行` : '通过 SSH 继续', () => {
        bridge('remoteResume', { id: session.id });
      }, { disabled: resumeBlocked });
      addAction('复制继续命令', () => bridge('remoteCopy', { id: session.id }), {
        disabled: resumeBlocked,
      });
      addAction('连接服务器', () => bridge('remoteConnect', { host: session.remoteHost }));
      if (writerActive) {
        addAction('结束原进程并继续…', () => {
          const tty = session.writerTty ? ` · ${session.writerTty}` : '';
          const confirmed = window.confirm(
            `这个会话仍在${owner}中运行\n\nPID ${session.pid}${tty}。继续会停止原会话及其正在执行的工具，未完成的工作可能会中断。`,
          );
          if (confirmed) bridge('terminateRemote', { id: session.id });
        });
      }
    } else {
      addAction(resumeBlocked ? `会话已在${owner}运行` : '在终端中继续', () => {
        bridge('resume', { id: session.id });
      }, { disabled: resumeBlocked });
      addAction('复制继续命令', () => bridge('copy', { id: session.id }), {
        disabled: resumeBlocked,
      });
      addAction('在 Finder 中显示项目', () => bridge('reveal', { id: session.id }), {
        disabled: !session.cwd,
      });
    }
    addSeparator();
    addAction('重命名…', () => openRenameModal({
      id: session.id,
      currentName: session.title || session.lastPrompt || '',
    }));
    addAction(deletionBlocked ? '运行中不可删除' : '删除会话…', () => {
      const confirmed = window.confirm('确定删除这个会话吗？\n\n会话会被归档并从列表中移除，原始会话日志不会被删除。');
      if (confirmed) bridge('archiveSession', { id: session.id });
    }, { disabled: deletionBlocked, danger: !deletionBlocked });

    elements.menu.hidden = true;
    elements.sessionContextMenu.replaceChildren(...items);
    elements.sessionContextMenu.hidden = false;
    const bounds = elements.sessionContextMenu.getBoundingClientRect();
    elements.sessionContextMenu.style.left = `${Math.max(6, Math.min(clientX, window.innerWidth - bounds.width - 6))}px`;
    elements.sessionContextMenu.style.top = `${Math.max(6, Math.min(clientY, window.innerHeight - bounds.height - 6))}px`;
  }

  function separator() {
    const element = document.createElement('span');
    element.className = 'meta-separator';
    element.textContent = '·';
    return element;
  }

  function actionButton(action, label, svg, id, details = {}, disabled = false) {
    const button = document.createElement('button');
    button.className = 'action-button';
    button.dataset.action = action;
    button.title = label;
    button.setAttribute('aria-label', label);
    button.disabled = disabled;
    button.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${svg}</svg>`;
    button.addEventListener('click', event => {
      event.stopPropagation();
      bridge(action, { id, ...details });
      if (action === 'copy' || action === 'remoteCopy') {
        const original = button.innerHTML;
        button.textContent = '✓';
        setTimeout(() => { button.innerHTML = original; }, 1000);
      }
    });
    return button;
  }

  function relativeTime(timestamp) {
    const seconds = Math.round((timestamp - Date.now()) / 1000);
    const formatter = new Intl.RelativeTimeFormat('zh-CN', { numeric: 'auto', style: 'narrow' });
    if (Math.abs(seconds) < 60) return formatter.format(seconds, 'second');
    const minutes = Math.round(seconds / 60);
    if (Math.abs(minutes) < 60) return formatter.format(minutes, 'minute');
    const hours = Math.round(minutes / 60);
    if (Math.abs(hours) < 24) return formatter.format(hours, 'hour');
    return formatter.format(Math.round(hours / 24), 'day');
  }

  document.querySelectorAll('[data-filter]').forEach(button => {
    button.addEventListener('click', () => { appState.filter = button.dataset.filter; render(); });
  });
  elements.search.addEventListener('input', () => { appState.query = elements.search.value; render(); });
  elements.refresh.addEventListener('click', () => {
    elements.refresh.classList.add('spinning');
    bridge('refresh');
  });
  elements.pinButton.addEventListener('click', () => {
    appState.windowPinned = !appState.windowPinned;
    render();
    bridge('setWindowPinned', { pinned: appState.windowPinned });
  });
  elements.minimizeButton.addEventListener('click', () => bridge('minimize'));
  elements.emptyAction.addEventListener('click', () => {
    const action = elements.emptyAction.dataset.action;
    if (action === 'remoteManage') openRemoteModal();
    else if (action) bridge(action, { host: elements.emptyAction.dataset.host || '' });
  });
  const openRemoteModal = () => {
    elements.remoteModal.hidden = false;
    elements.displaySettingsModal.hidden = true;
    elements.menu.hidden = true;
    bridge('reloadSSHHosts');
    scheduleWindowHeight();
    setTimeout(() => elements.remoteHostInput.focus(), 0);
  };
  const closeRemoteModal = () => {
    elements.remoteModal.hidden = true;
    scheduleWindowHeight();
  };
  const closeRenameModal = () => {
    elements.renameModal.hidden = true;
    delete elements.renameForm.dataset.sessionId;
  };
  const openRenameModal = payload => {
    if (!payload || typeof payload.id !== 'string') return;
    elements.remoteModal.hidden = true;
    elements.menu.hidden = true;
    elements.renameForm.dataset.sessionId = payload.id;
    elements.renameInput.value = typeof payload.currentName === 'string' ? payload.currentName : '';
    elements.renameModal.hidden = false;
    setTimeout(() => {
      elements.renameInput.focus();
      elements.renameInput.select();
    }, 0);
  };
  const closeDisplaySettings = () => {
    elements.displaySettingsModal.hidden = true;
    scheduleWindowHeight();
  };
  const updateFocusTotalPreview = () => {
    const total = [...elements.displaySettingsForm.querySelectorAll('[data-display-limit]')]
      .reduce((sum, input) => sum + normalizeDisplayLimit(input.value, 1), 0);
    elements.focusLimitTotal.textContent = `${total} 条`;
  };
  const openDisplaySettings = () => {
    elements.remoteModal.hidden = true;
    elements.menu.hidden = true;
    for (const input of elements.displaySettingsForm.querySelectorAll('[data-display-limit]')) {
      input.value = String(appState.displayLimits[input.dataset.displayLimit]);
    }
    updateFocusTotalPreview();
    elements.displaySettingsForm.dataset.titleLines = String(appState.titleLines);
    elements.displaySettingsForm.querySelectorAll('[data-title-lines]').forEach(button => {
      button.classList.toggle('selected', Number(button.dataset.titleLines) === appState.titleLines);
    });
    elements.displaySettingsModal.hidden = false;
    scheduleWindowHeight();
  };
  elements.manageRemoteMenu.addEventListener('click', openRemoteModal);
  elements.dragRegion.addEventListener('mousedown', event => {
    if (event.button !== 0) return;
    if (event.target.closest?.('button, input, select, textarea, a, [contenteditable="true"], .menu')) return;
    event.preventDefault();
    window.codexPulse?.startDragging();
  });
  elements.displaySettingsMenu.addEventListener('click', openDisplaySettings);
  elements.remoteModalClose.addEventListener('click', closeRemoteModal);
  elements.reloadSSHHosts.addEventListener('click', () => {
    elements.reloadSSHHosts.textContent = '读取中…';
    bridge('reloadSSHHosts');
  });
  elements.remoteModal.addEventListener('click', event => {
    if (event.target === elements.remoteModal) closeRemoteModal();
  });
  elements.remoteForm.addEventListener('submit', event => {
    event.preventDefault();
    const host = elements.remoteHostInput.value.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9._:@-]{0,254}$/.test(host)) {
      elements.remoteFormError.hidden = false;
      elements.remoteFormError.textContent = '请输入 SSH 别名或 user@host，不要包含空格或命令参数。';
      return;
    }
    appState.remoteConfigError = null;
    elements.remoteHostInput.value = '';
    bridge('addRemoteHost', { host });
  });
  elements.renameModalClose.addEventListener('click', closeRenameModal);
  elements.renameCancel.addEventListener('click', closeRenameModal);
  elements.renameModal.addEventListener('click', event => {
    if (event.target === elements.renameModal) closeRenameModal();
  });
  elements.renameForm.addEventListener('submit', event => {
    event.preventDefault();
    const id = elements.renameForm.dataset.sessionId;
    const name = elements.renameInput.value.replace(/\s+/gu, ' ').trim();
    if (!id || !name) return;
    closeRenameModal();
    bridge('renameSession', { id, name });
  });
  elements.displaySettingsClose.addEventListener('click', closeDisplaySettings);
  elements.displaySettingsCancel.addEventListener('click', closeDisplaySettings);
  elements.displaySettingsModal.addEventListener('click', event => {
    if (event.target === elements.displaySettingsModal) closeDisplaySettings();
  });
  elements.displaySettingsForm.querySelectorAll('[data-step]').forEach(button => {
    button.addEventListener('click', () => {
      const input = button.closest('.number-stepper')?.querySelector('input');
      if (!input) return;
      const next = Math.max(1, Math.min(8, (Number(input.value) || 1) + Number(button.dataset.step)));
      input.value = String(next);
      updateFocusTotalPreview();
    });
  });
  elements.displaySettingsForm.querySelectorAll('[data-display-limit]').forEach(input => {
    input.addEventListener('input', updateFocusTotalPreview);
  });
  elements.displaySettingsForm.querySelectorAll('[data-title-lines]').forEach(button => {
    button.addEventListener('click', () => {
      const titleLines = Number(button.dataset.titleLines) === 2 ? 2 : 1;
      elements.displaySettingsForm.dataset.titleLines = String(titleLines);
      elements.displaySettingsForm.querySelectorAll('[data-title-lines]').forEach(candidate => {
        candidate.classList.toggle('selected', candidate === button);
      });
    });
  });
  elements.displaySettingsForm.addEventListener('submit', event => {
    event.preventDefault();
    const displayLimits = { ...defaultDisplayLimits };
    for (const input of elements.displaySettingsForm.querySelectorAll('[data-display-limit]')) {
      const key = input.dataset.displayLimit;
      displayLimits[key] = normalizeDisplayLimit(input.value, appState.displayLimits[key]);
    }
    appState.displayLimits = displayLimits;
    appState.titleLines = Number(elements.displaySettingsForm.dataset.titleLines) === 2 ? 2 : 1;
    closeDisplaySettings();
    render();
    bridge('setDisplayPreferences', {
      displayLimits: appState.displayLimits,
      titleLines: appState.titleLines,
    });
  });
  elements.yoloToggle.addEventListener('click', event => {
    event.stopPropagation();
    appState.yoloEnabled = !appState.yoloEnabled;
    render();
    bridge('setYolo', { enabled: appState.yoloEnabled });
  });
  document.querySelectorAll('[data-session-title-mode]').forEach(button => {
    button.addEventListener('click', event => {
      event.stopPropagation();
      const mode = button.dataset.sessionTitleMode;
      if (mode !== 'prompt' && mode !== 'title') return;
      appState.sessionTitleMode = mode;
      render();
      elements.menu.hidden = true;
      bridge('setSessionTitleMode', { mode });
    });
  });
  elements.menuButton.addEventListener('click', event => {
    event.stopPropagation();
    closeSessionContextMenu();
    elements.menu.hidden = !elements.menu.hidden;
  });
  elements.sessionContextMenu.addEventListener('click', event => event.stopPropagation());
  elements.list.addEventListener('scroll', closeSessionContextMenu);
  document.addEventListener('click', () => {
    elements.menu.hidden = true;
    closeSessionContextMenu();
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    if (!elements.sessionContextMenu.hidden) closeSessionContextMenu();
    else if (!elements.displaySettingsModal.hidden) closeDisplaySettings();
    else if (!elements.renameModal.hidden) closeRenameModal();
    else if (!elements.remoteModal.hidden) closeRemoteModal();
  });
  document.querySelectorAll('[data-menu-action]').forEach(button => {
    button.addEventListener('click', () => bridge(button.dataset.menuAction));
  });
  window.codexPulse?.onState(receive);
  window.codexPulse?.onCommand(payload => {
    if (payload?.action === 'renameSession') openRenameModal(payload);
    else if (payload?.action === 'error') window.alert(payload.message || '操作失败');
  });
  bridge('ready');
})();
