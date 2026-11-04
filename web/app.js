'use strict';

/* =========================================================
   岗位 JD 分析 · 前端逻辑（原生 JS，无框架 / 无依赖）
   安全约定：所有接口内容一律走 textContent / DOM API 渲染，
   不使用 innerHTML 注入任何动态数据。
   ========================================================= */

var CONN_MSG = '无法连接本地服务，请确认已启动 server.py';
var POLL_INTERVAL = 800;
var DEFAULT_TYPES = [
  '技术负责人', '算法', '后端', '前端', '全栈', '客户端', '测试', '运维',
  '数据', '安全', '产品', '设计', '运营', '市场', '销售', '人力', '财务', '其他'
];
var STAGE_TEXT = {
  extract: '正在读取截图…',
  search: '正在联网搜索公司…',
  analyze: '正在生成分析…'
};

/* ---------------- 全局状态 ---------------- */

var state = {
  types: DEFAULT_TYPES.slice(),
  jobs: new Map(),            /* id -> Job */
  currentJobId: null,
  editingJobId: null,
  collapsed: Object.create(null),
  hasKey: false,
  submitting: false,
  jd: null,                   /* { dataUrl, name } */
  company: null
};

var pollers = new Map();      /* id -> { timer, fails } */
var toastTimer = null;
var dom = {};
var pasteTarget = 'jd';       /* Ctrl+V 粘贴的目标上传框：jd / company */

/* ---------------- 通用工具 ---------------- */

function $(selector) {
  return document.querySelector(selector);
}

function el(tag, options, children) {
  var opts = options || {};
  var node = document.createElement(tag);
  if (opts.className) node.className = opts.className;
  if (opts.text !== undefined && opts.text !== null) node.textContent = String(opts.text);
  if (opts.testid) node.setAttribute('data-testid', opts.testid);
  if (opts.attrs) {
    Object.keys(opts.attrs).forEach(function (name) {
      var value = opts.attrs[name];
      if (value !== null && value !== undefined) node.setAttribute(name, String(value));
    });
  }
  if (opts.on) {
    Object.keys(opts.on).forEach(function (eventName) {
      node.addEventListener(eventName, opts.on[eventName]);
    });
  }
  appendChildren(node, children);
  return node;
}

function appendChildren(node, children) {
  if (children === undefined || children === null) return;
  var list = Array.isArray(children) ? children : [children];
  list.forEach(function (child) {
    if (child === null || child === undefined || child === false) return;
    if (typeof child === 'string' || typeof child === 'number') {
      node.appendChild(document.createTextNode(String(child)));
    } else {
      node.appendChild(child);
    }
  });
}

function str(value) {
  return value === undefined || value === null ? '' : String(value);
}

function isKeyError(message) {
  return /api\s*key/i.test(str(message));
}

function safeHttpUrl(raw) {
  var value = str(raw).trim();
  if (!value) return '';
  try {
    var url = new URL(value, window.location.href);
    if (url.protocol === 'http:' || url.protocol === 'https:') return url.href;
  } catch (err) { /* 非法 URL */ }
  return '';
}

function hostOf(rawUrl) {
  try {
    return new URL(rawUrl, window.location.href).hostname;
  } catch (err) {
    return '';
  }
}

function safeImageSrc(raw) {
  var value = str(raw).trim();
  if (!value) return '';
  if (/^data:image\/[a-z0-9.+-]+;base64,/i.test(value)) return value;
  if (value.indexOf('api/images/') === 0 || value.indexOf('/api/images/') === 0) return value;
  return '';
}

function readFileAsDataURL(file) {
  return new Promise(function (resolve, reject) {
    var reader = new FileReader();
    reader.onload = function () { resolve(str(reader.result)); };
    reader.onerror = function () { reject(reader.error || new Error('图片读取失败')); };
    reader.readAsDataURL(file);
  });
}

/* ---------------- 接口请求 ---------------- */

/* 相对路径请求：同时支持「根路径部署」和「子路径部署」（如 https://example.com/jd/） */
function apiUrl(path) {
  var clean = str(path).replace(/^\/+/, '');
  return clean.indexOf('api/') === 0 ? clean : 'api/' + clean;
}

async function api(path, options) {
  var opts = options || {};
  var init = { method: opts.method || 'GET', headers: {} };
  if (opts.body !== undefined && opts.body !== null) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }

  var response;
  try {
    response = await fetch(apiUrl(path), init);
  } catch (err) {
    showNotice(CONN_MSG);
    var netError = new Error(CONN_MSG);
    netError.network = true;
    throw netError;
  }

  var data = null;
  try { data = await response.json(); } catch (err) { data = null; }

  if (!response.ok || (data && data.ok === false)) {
    var message = data && typeof data.error === 'string' && data.error
      ? data.error
      : '请求失败（HTTP ' + response.status + '）';
    var apiError = new Error(message);
    apiError.status = response.status;
    apiError.network = false;
    throw apiError;
  }

  if (dom.notice && !dom.notice.hidden && dom.noticeText.textContent === CONN_MSG) {
    hideNotice();
  }
  return data || { ok: true };
}

/* ---------------- 顶部提示 / Toast ---------------- */

function showNotice(message) {
  dom.noticeText.textContent = str(message);
  dom.notice.hidden = false;
}

function hideNotice() {
  dom.notice.hidden = true;
}

function showToast(message) {
  dom.toast.textContent = str(message);
  dom.toast.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(function () {
    dom.toast.hidden = true;
  }, 2600);
}

/* ---------------- 主题 ---------------- */

function getTheme() {
  return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
}

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
}

function syncThemeButton() {
  if (!dom.themeToggle) return;
  var label = getTheme() === 'dark' ? '切换到浅色主题' : '切换到深色主题';
  dom.themeToggle.setAttribute('aria-label', label);
  dom.themeToggle.setAttribute('title', label);
}

function toggleTheme() {
  var next = getTheme() === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  try { localStorage.setItem('theme', next); } catch (err) { /* 忽略 */ }
  syncThemeButton();
}

function wireSystemTheme() {
  if (!window.matchMedia) return;
  var query = window.matchMedia('(prefers-color-scheme: light)');
  var handler = function (event) {
    var stored = null;
    try { stored = localStorage.getItem('theme'); } catch (err) { /* 忽略 */ }
    if (stored === 'light' || stored === 'dark') return;
    applyTheme(event.matches ? 'light' : 'dark');
    syncThemeButton();
  };
  if (query.addEventListener) query.addEventListener('change', handler);
  else if (query.addListener) query.addListener(handler);
}

/* ---------------- 岗位列表 ---------------- */

function jobInfo(job) {
  return (job && job.job) || {};
}

function jobTypeOf(job) {
  var type = jobInfo(job).type;
  return type ? str(type) : '其他';
}

function orderedTypes() {
  var result = [];
  var seen = Object.create(null);
  state.types.forEach(function (type) {
    type = str(type);
    if (!type || seen[type]) return;
    seen[type] = true;
    result.push(type);
  });
  state.jobs.forEach(function (job) {
    var type = jobTypeOf(job);
    if (!seen[type]) {
      seen[type] = true;
      result.push(type);
    }
  });
  return result;
}

function statusMeta(job) {
  if (job.status === 'processing') return { className: 'status-processing', label: '分析中' };
  if (job.status === 'error') return { className: 'status-error', label: '失败' };
  return { className: 'status-done', label: '完成' };
}

function jobListItem(job) {
  var info = jobInfo(job);
  var title = info.title ? str(info.title) : '未命名岗位';
  var subParts = [];
  if (info.company) subParts.push(str(info.company));
  if (info.salary) subParts.push(str(info.salary));
  var status = statusMeta(job);
  var isActive = state.currentJobId === str(job.id);

  var button = el('button', {
    type: 'button',
    className: 'job-item' + (isActive ? ' active' : ''),
    testid: 'job-item',
    attrs: { 'data-job-id': str(job.id) }
  }, [
    el('span', { className: 'job-item-title', text: title }),
    subParts.length ? el('span', { className: 'job-item-sub', text: subParts.join(' · ') }) : null,
    el('span', {
      className: 'status ' + status.className,
      attrs: { 'aria-label': '状态：' + status.label }
    }, [
      el('span', { className: 'status-dot', attrs: { 'aria-hidden': 'true' } }),
      el('span', { text: status.label })
    ])
  ]);

  button.addEventListener('click', function () { openJob(str(job.id)); });
  return button;
}

function renderJobList(loadFailed) {
  var all = [];
  state.jobs.forEach(function (job) { all.push(job); });

  if (!all.length) {
    dom.jobList.replaceChildren(el('p', {
      className: 'muted empty-list',
      text: loadFailed ? '岗位列表加载失败' : '暂无岗位，点击上方按钮添加'
    }));
    return;
  }

  var groups = new Map();
  orderedTypes().forEach(function (type) { groups.set(type, []); });
  all.forEach(function (job) {
    var type = jobTypeOf(job);
    if (!groups.has(type)) groups.set(type, []);
    groups.get(type).push(job);
  });

  var fragment = document.createDocumentFragment();
  groups.forEach(function (items, type) {
    if (!items.length) return;
    var collapsed = !!state.collapsed[type];
    var head = el('button', {
      type: 'button',
      className: 'group-head' + (collapsed ? ' collapsed' : ''),
      attrs: { 'aria-expanded': collapsed ? 'false' : 'true' }
    }, [
      el('span', { className: 'group-arrow', text: '▾', attrs: { 'aria-hidden': 'true' } }),
      el('span', { className: 'group-name', text: type }),
      el('span', { className: 'group-count', text: String(items.length) })
    ]);
    head.addEventListener('click', function () {
      state.collapsed[type] = !state.collapsed[type];
      renderJobList();
    });
    fragment.appendChild(head);

    if (!collapsed) {
      var wrap = el('div', { className: 'group-items' });
      items.forEach(function (job) { wrap.appendChild(jobListItem(job)); });
      fragment.appendChild(wrap);
    }
  });
  dom.jobList.replaceChildren(fragment);
}

/* ---------------- 视图切换 ---------------- */

function showAddView() {
  state.currentJobId = null;
  dom.viewJob.hidden = true;
  dom.viewAdd.hidden = false;
  renderJobList();
  updateSubmitState();
}

function ensureJobView() {
  dom.viewAdd.hidden = true;
  dom.viewJob.hidden = false;
}

function renderJobLoading() {
  ensureJobView();
  dom.viewJob.replaceChildren(el('div', { className: 'stage-view' }, [
    el('div', { className: 'spinner', attrs: { 'aria-hidden': 'true' } }),
    el('p', { className: 'stage-text', text: '加载中…' })
  ]));
}

function renderJob(job) {
  if (!job) return;
  if (job.status === 'processing') {
    renderStage(job);
    return;
  }
  if (job.status === 'error') {
    renderErrorJob(job);
    return;
  }
  renderDetail(job);
}

/* ---------------- B. 分析中 ---------------- */

function renderStage(job) {
  ensureJobView();
  var message = STAGE_TEXT[job.stage] || '正在处理…';
  var existing = dom.viewJob.querySelector('.stage-view');

  if (existing && existing.getAttribute('data-job-id') === str(job.id)) {
    var stageText = existing.querySelector('[data-testid="stage-text"]');
    if (stageText) stageText.textContent = message;
    return;
  }

  dom.viewJob.replaceChildren(el('div', {
    className: 'stage-view',
    attrs: { 'data-job-id': str(job.id) }
  }, [
    el('div', { className: 'spinner', attrs: { 'aria-hidden': 'true' } }),
    el('h2', { text: '正在分析岗位' }),
    el('p', {
      className: 'stage-text',
      testid: 'stage-text',
      text: message,
      attrs: { 'aria-live': 'polite' }
    }),
    el('p', {
      className: 'stage-hint',
      text: '分析期间可以切换左侧其他岗位查看，完成后结果会自动更新。'
    })
  ]));
}

/* ---------------- C. 岗位详情 ---------------- */

function paragraphNodes(value) {
  var source = str(value).trim();
  if (!source) return [];
  return source.split(/\r?\n\s*\r?\n/).map(function (chunk) {
    return chunk.trim();
  }).filter(Boolean).map(function (chunk) {
    return el('p', { className: 'para', text: chunk });
  });
}

function talentNodes(value) {
  var source = str(value);
  var lines = source.split(/\r?\n/);
  var nodes = [];
  var currentList = null;

  lines.forEach(function (rawLine) {
    var line = rawLine.trim();
    if (!line) {
      if (currentList) {
        nodes.push(currentList);
        currentList = null;
      }
      return;
    }
    var match = line.match(/^[-–—•*]\s*(.+)$/);
    if (match) {
      if (!currentList) currentList = el('ul', { className: 'list' });
      currentList.appendChild(el('li', { text: match[1] }));
    } else {
      if (currentList) {
        nodes.push(currentList);
        currentList = null;
      }
      nodes.push(el('p', { className: 'para', text: line }));
    }
  });

  if (currentList) nodes.push(currentList);
  return nodes;
}

function listNode(items) {
  if (!Array.isArray(items) || !items.length) return null;
  return el('ul', { className: 'list' }, items.map(function (item) {
    return el('li', { text: str(item) });
  }));
}

function sectionNode(title, children) {
  return el('section', { className: 'section' }, [
    el('h2', { className: 'section-title', text: title })
  ].concat(children));
}

function metaChips(values) {
  var cleaned = values.map(str).filter(Boolean);
  if (!cleaned.length) return null;
  return el('div', { className: 'meta-row' }, cleaned.map(function (value) {
    return el('span', { className: 'meta-chip', text: value });
  }));
}

function createReanalyzeButton(jobId) {
  var button = el('button', {
    type: 'button',
    className: 'btn',
    testid: 'reanalyze-btn',
    text: '重新分析',
    attrs: { 'data-job-id': str(jobId) }
  });
  button.addEventListener('click', function () { reanalyzeJob(str(jobId), button); });
  return button;
}

function createDeleteButton(jobId) {
  var button = el('button', {
    type: 'button',
    className: 'btn danger',
    testid: 'delete-job',
    text: '删除',
    attrs: { 'data-job-id': str(jobId) }
  });
  button.addEventListener('click', function () { deleteJob(str(jobId)); });
  return button;
}

function createEditButton(jobId) {
  var button = el('button', {
    type: 'button',
    className: 'btn',
    testid: 'edit-job',
    text: '修正信息',
    attrs: { 'data-job-id': str(jobId) }
  });
  button.addEventListener('click', function () {
    var job = state.jobs.get(str(jobId));
    if (job) openEditJob(job);
  });
  return button;
}

function renderDetail(job) {
  ensureJobView();
  var info = jobInfo(job);
  var company = job.company || {};
  var analysis = job.analysis || {};
  var title = info.title ? str(info.title) : '未命名岗位';
  var type = info.type ? str(info.type) : '其他';

  var subParts = [];
  if (info.company) subParts.push(str(info.company));
  if (info.salary) subParts.push(str(info.salary));

  var header = el('header', { className: 'detail-head' }, [
    el('div', { className: 'detail-title-row' }, [
      el('h1', { className: 'job-title', testid: 'job-title', text: title }),
      el('span', { className: 'badge', testid: 'job-type', text: type })
    ]),
    subParts.length ? el('p', { className: 'detail-sub', text: subParts.join(' · ') }) : null,
    metaChips([info.city, info.experience, info.education]),
    el('div', { className: 'detail-actions' }, [
      createEditButton(job.id),
      createReanalyzeButton(job.id),
      createDeleteButton(job.id)
    ])
  ]);

  /* 1. 岗位信息 */
  var infoChildren = [];
  var tags = Array.isArray(info.tags) ? info.tags : [];
  if (tags.length) {
    infoChildren.push(el('div', { className: 'chips' }, tags.map(function (tag) {
      return el('span', { className: 'chip', text: str(tag) });
    })));
  }
  if (info.summary) infoChildren.push(el('p', { className: 'summary', text: str(info.summary) }));
  var responsibilities = listNode(info.responsibilities);
  if (responsibilities) {
    infoChildren.push(el('h3', { className: 'sub-title', text: '岗位职责' }));
    infoChildren.push(responsibilities);
  }
  var requirements = listNode(info.requirements);
  if (requirements) {
    infoChildren.push(el('h3', { className: 'sub-title', text: '任职要求' }));
    infoChildren.push(requirements);
  }
  var bonusPoints = listNode(info.bonusPoints);
  if (bonusPoints) {
    infoChildren.push(el('h3', { className: 'sub-title', text: '加分项' }));
    infoChildren.push(bonusPoints);
  }
  if (!infoChildren.length) infoChildren.push(el('p', { className: 'muted', text: '暂无岗位信息' }));
  var infoSection = sectionNode('岗位信息', infoChildren);

  /* 2. 公司详情 */
  var companyChildren = [];
  var industryMeta = metaChips([company.industry, company.size, company.stage]);
  if (industryMeta) companyChildren.push(industryMeta);
  if (company.description) companyChildren.push(el('p', { className: 'para', text: str(company.description) }));
  var highlights = listNode(company.highlights);
  if (highlights) {
    companyChildren.push(el('h3', { className: 'sub-title', text: '公司亮点' }));
    companyChildren.push(highlights);
  }
  if (!companyChildren.length) companyChildren.push(el('p', { className: 'muted', text: '暂无公司信息' }));
  var companySection = sectionNode('公司详情', companyChildren);

  /* 3. 分析结果 */
  var doingBody = el('div', { className: 'analysis-body' }, paragraphNodes(analysis.doing));
  if (!doingBody.firstChild) doingBody.appendChild(el('p', { className: 'muted', text: '暂无分析结果' }));
  var doingCard = el('article', { className: 'analysis-card', testid: 'doing-card' }, [
    el('h2', { text: '公司在做什么' }),
    doingBody
  ]);

  var talentBody = el('div', { className: 'analysis-body' }, talentNodes(analysis.talent));
  if (!talentBody.firstChild) talentBody.appendChild(el('p', { className: 'muted', text: '暂无分析结果' }));
  var talentCard = el('article', { className: 'analysis-card', testid: 'talent-card' }, [
    el('h2', { text: '公司需要什么样的人才' }),
    talentBody
  ]);

  var analysisWrap = el('section', { className: 'analysis-wrap' }, [
    el('h2', { className: 'section-title', text: '分析结果' }),
    el('div', { className: 'analysis-grid' }, [doingCard, talentCard])
  ]);

  /* 4. 参考资料 */
  var sources = Array.isArray(analysis.sources) ? analysis.sources : [];
  var sourceList = el('ul', { className: 'sources', testid: 'sources' });
  sources.forEach(function (source) {
    if (!source) return;
    var rawUrl = str(source.url);
    var url = safeHttpUrl(rawUrl);
    var sourceTitle = source.title ? str(source.title) : (url || rawUrl || '未命名来源');
    var item = el('li');
    if (url) {
      item.appendChild(el('a', {
        text: sourceTitle,
        attrs: { href: url, target: '_blank', rel: 'noopener noreferrer' }
      }));
      var host = hostOf(url);
      if (host) item.appendChild(el('span', { className: 'source-domain', text: host }));
    } else {
      item.appendChild(el('span', { className: 'muted', text: sourceTitle }));
    }
    sourceList.appendChild(item);
  });
  if (!sources.length) {
    sourceList.appendChild(el('li', { className: 'muted', text: '暂无参考资料' }));
  }
  var sourcesSection = sectionNode('参考资料', [sourceList]);

  /* 5. 原始截图 */
  var images = job.images || {};
  var thumbs = [];
  [images.jd, images.company].forEach(function (rawSrc) {
    var src = safeImageSrc(rawSrc);
    if (!src) return;
    var thumb = el('button', {
      type: 'button',
      className: 'thumb',
      testid: 'image-toggle',
      attrs: { 'aria-label': '查看原始截图' }
    }, [el('img', { attrs: { src: src, alt: '原始截图' } })]);
    thumb.addEventListener('click', function () { openImage(src); });
    thumbs.push(thumb);
  });
  var shotsSection = sectionNode('原始截图', thumbs.length
    ? [el('div', { className: 'thumbs' }, thumbs)]
    : [el('p', { className: 'muted', text: '暂无截图' })]);

  dom.viewJob.replaceChildren(el('div', { className: 'detail' }, [
    header,
    infoSection,
    companySection,
    analysisWrap,
    sourcesSection,
    shotsSection
  ]));
}

/* ---------------- D. 失败状态 ---------------- */

function renderErrorJob(job) {
  ensureJobView();
  var info = jobInfo(job);
  var title = info.title ? str(info.title) : '未命名岗位';
  var type = info.type ? str(info.type) : '其他';
  var message = job.error ? str(job.error) : '分析失败，请重试。';

  var actions = el('div', { className: 'detail-actions' }, [
    createReanalyzeButton(job.id),
    createDeleteButton(job.id)
  ]);
  if (isKeyError(message)) {
    var settingsButton = el('button', {
      type: 'button',
      className: 'btn',
      text: '打开设置'
    });
    settingsButton.addEventListener('click', function () {
      openSettings({ result: message });
    });
    actions.appendChild(settingsButton);
  }

  dom.viewJob.replaceChildren(el('div', { className: 'detail' }, [
    el('header', { className: 'detail-head' }, [
      el('div', { className: 'detail-title-row' }, [
        el('h1', { className: 'job-title', testid: 'job-title', text: title }),
        el('span', { className: 'badge', testid: 'job-type', text: type })
      ]),
      el('p', { className: 'detail-sub muted', text: '分析失败' })
    ]),
    el('section', { className: 'section' }, [
      el('h2', { className: 'section-title', text: '分析失败' }),
      el('div', { className: 'error-box', text: message }),
      actions
    ])
  ]));
}

/* ---------------- 截图全屏 ---------------- */

function openImage(src) {
  dom.overlayImg.setAttribute('src', src);
  dom.imageOverlay.hidden = false;
}

function closeImage() {
  dom.imageOverlay.hidden = true;
  dom.overlayImg.removeAttribute('src');
}

/* ---------------- 添加岗位 / 上传 ---------------- */

var uploadEls = {};

function cacheUploadEls() {
  uploadEls.jd = {
    dropzone: $('#jd-dropzone'),
    input: $('#jd-input'),
    preview: $('#jd-preview'),
    img: $('#jd-preview-img'),
    name: $('#jd-file-name'),
    remove: $('#jd-remove'),
    section: $('#jd-upload')
  };
  uploadEls.company = {
    dropzone: $('#company-dropzone'),
    input: $('#company-input'),
    preview: $('#company-preview'),
    img: $('#company-preview-img'),
    name: $('#company-file-name'),
    remove: $('#company-remove'),
    section: $('#company-upload')
  };
}

function renderUpload(kind) {
  var els = uploadEls[kind];
  if (!els) return;
  var value = state[kind];
  if (value) {
    els.img.setAttribute('src', value.dataUrl);
    els.name.textContent = value.name;
    els.dropzone.hidden = true;
    els.preview.hidden = false;
  } else {
    els.img.removeAttribute('src');
    els.name.textContent = '';
    els.dropzone.hidden = false;
    els.preview.hidden = true;
  }
  updateSubmitState();
}

async function handleFile(kind, file) {
  if (!file) return;
  if (!file.type || file.type.indexOf('image/') !== 0) {
    showToast('请选择图片文件');
    return;
  }
  try {
    var dataUrl = await readFileAsDataURL(file);
    state[kind] = { dataUrl: dataUrl, name: file.name || '未命名图片' };
    renderUpload(kind);
  } catch (err) {
    showToast('图片读取失败，请重试');
  }
}

function updateSubmitState() {
  if (!dom.submitBtn) return;
  var hasJd = !!state.jd;
  dom.submitBtn.disabled = !hasJd || state.submitting;
  dom.submitHint.textContent = hasJd
    ? (state.submitting ? '正在提交…' : '提交后将自动提取岗位信息并联网分析')
    : '需先选择岗位 JD 截图';
}

function resetAddForm() {
  state.jd = null;
  state.company = null;
  if (uploadEls.jd) uploadEls.jd.input.value = '';
  if (uploadEls.company) uploadEls.company.input.value = '';
  renderUpload('jd');
  renderUpload('company');
}

async function submitJob() {
  if (!state.jd || state.submitting) return;
  state.submitting = true;
  updateSubmitState();
  var originalLabel = dom.submitBtn.textContent;
  dom.submitBtn.textContent = '提交中…';

  try {
    var data = await api('/api/jobs', {
      method: 'POST',
      body: {
        jdImage: state.jd.dataUrl,
        companyImage: state.company ? state.company.dataUrl : null
      }
    });
    if (!data.job) throw new Error('创建岗位失败');
    state.jobs.set(str(data.job.id), data.job);
    resetAddForm();
    renderJobList();
    if (data.job.status === 'processing') ensurePolling(str(data.job.id));
    openJob(str(data.job.id));
    showToast('已提交，正在分析…');
  } catch (err) {
    if (!err.network) {
      showNotice(err.message);
      if (isKeyError(err.message)) openSettings({ result: err.message });
    }
  } finally {
    state.submitting = false;
    dom.submitBtn.textContent = originalLabel;
    updateSubmitState();
  }
}

/* ---------------- 打开岗位 / 轮询 ---------------- */

async function openJob(jobId) {
  jobId = str(jobId);
  if (!jobId) return;
  state.currentJobId = jobId;
  renderJobList();

  var cached = state.jobs.get(jobId);
  if (cached) renderJob(cached);
  else renderJobLoading();

  if (cached && cached.status === 'processing') ensurePolling(jobId);

  try {
    var data = await api('/api/jobs/' + encodeURIComponent(jobId));
    if (!data.job) return;
    state.jobs.set(str(data.job.id), data.job);
    renderJobList();
    if (state.currentJobId !== jobId) return;
    renderJob(data.job);
    if (data.job.status === 'processing') ensurePolling(jobId);
  } catch (err) {
    if (state.currentJobId !== jobId) return;
    if (!err.network) showNotice(err.message);
  }
}

function ensurePolling(jobId) {
  jobId = str(jobId);
  if (!jobId || pollers.has(jobId)) return;
  var poller = { timer: null, fails: 0 };
  pollers.set(jobId, poller);
  poller.timer = setTimeout(function () { pollOnce(jobId); }, POLL_INTERVAL);
}

function stopPoller(jobId) {
  var poller = pollers.get(str(jobId));
  if (poller) {
    if (poller.timer) clearTimeout(poller.timer);
    pollers.delete(str(jobId));
  }
}

async function pollOnce(jobId) {
  var poller = pollers.get(jobId);
  if (!poller) return;
  try {
    var data = await api('/api/jobs/' + encodeURIComponent(jobId));
    if (!pollers.has(jobId)) return;
    if (!data.job) {
      pollers.delete(jobId);
      return;
    }
    state.jobs.set(str(data.job.id), data.job);
    renderJobList();
    if (state.currentJobId === jobId) renderJob(data.job);
    if (data.job.status === 'processing') {
      poller.fails = 0;
      poller.timer = setTimeout(function () { pollOnce(jobId); }, POLL_INTERVAL);
    } else {
      pollers.delete(jobId);
    }
  } catch (err) {
    if (!pollers.has(jobId)) return;
    poller.fails += 1;
    if (poller.fails >= 10) {
      pollers.delete(jobId);
      return;
    }
    poller.timer = setTimeout(function () { pollOnce(jobId); }, POLL_INTERVAL * 2);
  }
}

/* ---------------- 重新分析 / 删除 ---------------- */

async function reanalyzeJob(jobId, button) {
  if (button) button.disabled = true;
  try {
    var data = await api('/api/jobs/' + encodeURIComponent(jobId) + '/reanalyze', { method: 'POST' });
    if (data.job) state.jobs.set(str(data.job.id), data.job);
    renderJobList();
    var job = data.job || state.jobs.get(str(jobId));
    if (job && state.currentJobId === str(jobId)) renderJob(job);
    if (job && job.status === 'processing') ensurePolling(str(job.id));
    showToast('已重新开始分析');
  } catch (err) {
    if (!err.network) {
      showNotice(err.message);
      if (isKeyError(err.message)) openSettings({ result: err.message });
    }
  } finally {
    if (button && button.isConnected) button.disabled = false;
  }
}

async function deleteJob(jobId) {
  if (!window.confirm('确定删除该岗位吗？删除后无法恢复。')) return;
  try {
    await api('/api/jobs/' + encodeURIComponent(jobId), { method: 'DELETE' });
    stopPoller(jobId);
    state.jobs.delete(str(jobId));
    if (state.editingJobId === str(jobId)) closeEditJob();
    if (state.currentJobId === str(jobId)) {
      showAddView();
    } else {
      renderJobList();
    }
    showToast('已删除岗位');
  } catch (err) {
    if (!err.network) showNotice(err.message);
  }
}

/* ---------------- 修正信息（PATCH） ---------------- */

function fillEditTypeOptions(selected) {
  var types = state.types.slice();
  var value = selected ? str(selected) : '其他';
  if (types.indexOf(value) === -1) types.push(value);
  dom.editType.replaceChildren();
  types.forEach(function (type) {
    var option = document.createElement('option');
    option.value = type;
    option.textContent = type;
    if (type === value) option.selected = true;
    dom.editType.appendChild(option);
  });
}

function setEditResult(message, kind) {
  dom.editResult.textContent = str(message);
  dom.editResult.className = 'field-note' + (kind ? ' ' + kind : '');
}

function openEditJob(job) {
  if (!job) return;
  state.editingJobId = str(job.id);
  var info = jobInfo(job);
  dom.editTitle.value = info.title ? str(info.title) : '';
  dom.editCompany.value = info.company ? str(info.company) : '';
  fillEditTypeOptions(info.type ? str(info.type) : '其他');
  setEditResult('', '');
  dom.editModal.hidden = false;
  setTimeout(function () {
    try { dom.editTitle.focus(); } catch (err) { /* 忽略 */ }
  }, 0);
}

function closeEditJob() {
  state.editingJobId = null;
  dom.editModal.hidden = true;
  setEditResult('', '');
}

async function saveEditJob() {
  var jobId = state.editingJobId;
  if (!jobId) return;
  var payload = {
    title: dom.editTitle.value.trim(),
    company: dom.editCompany.value.trim(),
    type: dom.editType.value
  };
  var originalLabel = dom.editSave.textContent;
  dom.editSave.disabled = true;
  dom.editSave.textContent = '保存中…';
  try {
    var data = await api('/api/jobs/' + encodeURIComponent(jobId), {
      method: 'PATCH',
      body: payload
    });
    if (data.job) {
      state.jobs.set(str(data.job.id), data.job);
      renderJobList();
      if (state.currentJobId === str(jobId)) renderJob(data.job);
    }
    closeEditJob();
    showToast('信息已更新，可点「重新分析」按新公司名重新搜索');
  } catch (err) {
    if (err.network) setEditResult(CONN_MSG, 'error');
    else setEditResult(err.message, 'error');
  } finally {
    dom.editSave.disabled = false;
    dom.editSave.textContent = originalLabel;
  }
}

/* ---------------- 设置弹层 ---------------- */

function setSettingsResult(message, kind) {
  dom.settingsResult.textContent = str(message);
  dom.settingsResult.className = 'field-note' + (kind ? ' ' + kind : '');
}

async function openSettings(options) {
  var opts = options || {};
  dom.settingsModal.hidden = false;
  dom.settingsKey.value = '';
  dom.settingsKeyHint.textContent = '读取设置中…';
  dom.settingsBaseUrl.value = '';
  setSettingsResult(opts.result ? str(opts.result) : '', opts.result ? 'error' : '');

  setTimeout(function () {
    try { dom.settingsKey.focus(); } catch (err) { /* 忽略 */ }
  }, 0);

  try {
    var data = await api('/api/settings');
    state.hasKey = !!data.hasKey;
    var hint = data.keyHint ? str(data.keyHint) : '';
    dom.settingsKey.placeholder = state.hasKey
      ? '已保存 ' + (hint || '') + '，如需更换请输入新 Key'
      : 'sk-...';
    dom.settingsKeyHint.textContent = state.hasKey
      ? '已保存：' + (hint || '（已配置）')
      : '尚未保存 API Key';
    dom.settingsBaseUrl.value = data.baseUrl ? str(data.baseUrl) : '';
  } catch (err) {
    dom.settingsKeyHint.textContent = '读取设置失败：' + err.message;
  }
}

function closeSettings() {
  dom.settingsModal.hidden = true;
  dom.settingsKey.value = '';
  setSettingsResult('', '');
}

async function testSettings() {
  var key = dom.settingsKey.value.trim();
  if (!key && !state.hasKey) {
    setSettingsResult('请先填写 API Key', 'error');
    return;
  }
  var originalLabel = dom.settingsTest.textContent;
  dom.settingsTest.disabled = true;
  dom.settingsTest.textContent = '测试中…';
  setSettingsResult('正在测试连接…', '');
  try {
    var payload = {};
    if (key) payload.apiKey = key;
    var data = await api('/api/settings/test', { method: 'POST', body: payload });
    var models = Array.isArray(data.models) ? data.models.map(str).filter(Boolean).join('、') : '';
    setSettingsResult('连接成功' + (models ? '，可用模型：' + models : ''), 'success');
  } catch (err) {
    setSettingsResult(err.message, 'error');
  } finally {
    dom.settingsTest.disabled = false;
    dom.settingsTest.textContent = originalLabel;
  }
}

async function saveSettings() {
  var key = dom.settingsKey.value.trim();
  var baseUrl = dom.settingsBaseUrl.value.trim();
  if (!key && !state.hasKey) {
    setSettingsResult('请填写 API Key', 'error');
    return;
  }
  var originalLabel = dom.settingsSave.textContent;
  dom.settingsSave.disabled = true;
  dom.settingsSave.textContent = '保存中…';
  try {
    var payload = { baseUrl: baseUrl };
    if (key) payload.apiKey = key;
    var data = await api('/api/settings', { method: 'POST', body: payload });
    state.hasKey = !!data.hasKey;
    var hint = data.keyHint ? str(data.keyHint) : '';
    dom.settingsKey.value = '';
    dom.settingsKeyHint.textContent = state.hasKey
      ? '已保存：' + (hint || '（已配置）')
      : '尚未保存 API Key';
    closeSettings();
    showToast('设置已保存');
  } catch (err) {
    setSettingsResult(err.message, 'error');
  } finally {
    dom.settingsSave.disabled = false;
    dom.settingsSave.textContent = originalLabel;
  }
}

/* ---------------- 岗位数据加载 ---------------- */

async function loadJobs() {
  try {
    var data = await api('/api/jobs');
    if (Array.isArray(data.types) && data.types.length) {
      state.types = data.types.map(str).filter(Boolean);
    }
    state.jobs.clear();
    var jobs = Array.isArray(data.jobs) ? data.jobs : [];
    jobs.forEach(function (job) {
      if (job && job.id) state.jobs.set(str(job.id), job);
    });
    renderJobList();
    jobs.forEach(function (job) {
      if (job && job.status === 'processing') ensurePolling(str(job.id));
    });
    return true;
  } catch (err) {
    renderJobList(true);
    return false;
  }
}

/* ---------------- 事件绑定 ---------------- */

function bindEvents() {
  dom.addJobBtn.addEventListener('click', showAddView);
  dom.themeToggle.addEventListener('click', toggleTheme);
  dom.noticeClose.addEventListener('click', hideNotice);

  /* 添加岗位 */
  dom.submitBtn.addEventListener('click', submitJob);
  dom.jdInput.addEventListener('change', function () {
    var file = this.files && this.files[0];
    if (file) handleFile('jd', file);
    this.value = '';
  });
  dom.companyInput.addEventListener('change', function () {
    var file = this.files && this.files[0];
    if (file) handleFile('company', file);
    this.value = '';
  });
  uploadEls.jd.remove.addEventListener('click', function () {
    state.jd = null;
    uploadEls.jd.input.value = '';
    renderUpload('jd');
  });
  uploadEls.company.remove.addEventListener('click', function () {
    state.company = null;
    uploadEls.company.input.value = '';
    renderUpload('company');
  });
  bindDropzone(uploadEls.jd.section, 'jd');
  bindDropzone(uploadEls.company.section, 'company');
  /* 点过哪个上传框，Ctrl+V 就粘到哪个；默认粘到第一个空槽 */
  uploadEls.jd.section.addEventListener('click', function () { pasteTarget = 'jd'; });
  uploadEls.company.section.addEventListener('click', function () { pasteTarget = 'company'; });
  document.addEventListener('paste', onPaste);

  /* 设置弹层 */
  dom.settingsBtn.addEventListener('click', function () { openSettings(); });
  dom.settingsClose.addEventListener('click', closeSettings);
  dom.settingsBackdrop.addEventListener('click', closeSettings);
  dom.settingsSave.addEventListener('click', saveSettings);
  dom.settingsTest.addEventListener('click', testSettings);

  /* 修正信息弹层 */
  dom.editClose.addEventListener('click', closeEditJob);
  dom.editCancel.addEventListener('click', closeEditJob);
  dom.editBackdrop.addEventListener('click', closeEditJob);
  dom.editSave.addEventListener('click', saveEditJob);

  /* 截图遮罩 */
  dom.overlayClose.addEventListener('click', closeImage);
  dom.imageOverlay.addEventListener('click', function (event) {
    if (event.target === dom.imageOverlay) closeImage();
  });

  /* 键盘 */
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') return;
    if (!dom.imageOverlay.hidden) {
      closeImage();
      return;
    }
    if (!dom.editModal.hidden) {
      closeEditJob();
      return;
    }
    if (!dom.settingsModal.hidden) closeSettings();
  });

  /* 阻止浏览器直接打开拖进来的图片 */
  document.addEventListener('dragover', function (event) { event.preventDefault(); });
  document.addEventListener('drop', function (event) { event.preventDefault(); });
}

function bindDropzone(zone, kind) {
  if (!zone) return;
  ['dragenter', 'dragover'].forEach(function (eventName) {
    zone.addEventListener(eventName, function (event) {
      event.preventDefault();
      event.stopPropagation();
      if (uploadEls[kind].dropzone) uploadEls[kind].dropzone.classList.add('dragover');
    });
  });
  ['dragleave', 'dragend'].forEach(function (eventName) {
    zone.addEventListener(eventName, function (event) {
      event.preventDefault();
      if (uploadEls[kind].dropzone) uploadEls[kind].dropzone.classList.remove('dragover');
    });
  });
  zone.addEventListener('drop', function (event) {
    event.preventDefault();
    event.stopPropagation();
    if (uploadEls[kind].dropzone) uploadEls[kind].dropzone.classList.remove('dragover');
    var files = event.dataTransfer && event.dataTransfer.files;
    if (files && files.length) handleFile(kind, files[0]);
  });
}

/* Ctrl+V 粘贴截图：仅在「添加岗位」视图生效；优先粘到刚才点击的上传框 */
function onPaste(event) {
  if (!dom.viewAdd || dom.viewAdd.hidden) return;
  if ((dom.settingsModal && !dom.settingsModal.hidden) ||
      (dom.editModal && !dom.editModal.hidden)) return;
  var tag = ((event.target && event.target.tagName) || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return;

  var items = (event.clipboardData && event.clipboardData.items) || [];
  var file = null;
  for (var i = 0; i < items.length; i += 1) {
    if (items[i].kind === 'file' && items[i].type && items[i].type.indexOf('image/') === 0) {
      file = items[i].getAsFile();
      if (file) break;
    }
  }
  if (!file) return;
  event.preventDefault();

  var target = pasteTarget;
  if (state[target]) {
    target = !state.jd ? 'jd' : (!state.company ? 'company' : '');
  }
  if (!target) {
    showToast('两个上传框都已选图，请先移除一张再粘贴');
    return;
  }
  handleFile(target, file);
  pasteTarget = target === 'jd' ? 'company' : 'jd';
}

/* ---------------- 初始化 ---------------- */

function cacheDom() {
  dom.notice = $('#notice');
  dom.noticeText = $('#notice-text');
  dom.noticeClose = $('#notice-close');
  dom.themeToggle = $('#theme-toggle');
  dom.settingsBtn = $('#settings-btn');
  dom.addJobBtn = $('#add-job-btn');
  dom.jobList = $('#job-list');
  dom.viewAdd = $('#view-add');
  dom.viewJob = $('#view-job');
  dom.submitBtn = $('#submit-job');
  dom.submitHint = $('#submit-hint');
  dom.jdInput = $('#jd-input');
  dom.companyInput = $('#company-input');

  dom.settingsModal = $('#settings-modal');
  dom.settingsBackdrop = $('#settings-backdrop');
  dom.settingsClose = $('#settings-close');
  dom.settingsKey = $('#settings-key');
  dom.settingsKeyHint = $('#settings-key-hint');
  dom.settingsBaseUrl = $('#settings-base-url');
  dom.settingsTest = $('#settings-test');
  dom.settingsSave = $('#settings-save');
  dom.settingsResult = $('#settings-result');

  dom.editModal = $('#edit-modal');
  dom.editBackdrop = $('#edit-backdrop');
  dom.editClose = $('#edit-close');
  dom.editCancel = $('#edit-cancel');
  dom.editTitle = $('#edit-title');
  dom.editCompany = $('#edit-company');
  dom.editType = $('#edit-type');
  dom.editSave = $('#edit-save');
  dom.editResult = $('#edit-result');

  dom.imageOverlay = $('#image-overlay');
  dom.overlayImg = $('#overlay-img');
  dom.overlayClose = $('#overlay-close');
  dom.toast = $('#toast');

  cacheUploadEls();
}

async function init() {
  cacheDom();
  bindEvents();
  syncThemeButton();
  wireSystemTheme();
  renderUpload('jd');
  renderUpload('company');
  dom.jobList.replaceChildren(el('p', { className: 'muted empty-list', text: '加载中…' }));
  showAddView();
  await loadJobs();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
