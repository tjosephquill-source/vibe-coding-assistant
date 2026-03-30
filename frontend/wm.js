/**
 * Window Management System for VibeCodingAssistant
 *
 * IDE-style panel management:
 *   - Resizable split panes (drag splitters)
 *   - Close / reopen any panel
 *   - Floating (pop-out) windows
 *   - Drag-to-dock panel rearrangement
 *   - Tab groups (stack panels in same slot)
 *   - Layout state persisted to localStorage
 *   - Scalable panel registry — add new panels with one call
 */

/* ================================================================
   LAYOUT CONFIGURATIONS — named presets
   ================================================================ */
const WM_LAYOUT_CONFIGS = {
  'Default': {
    layout: {
      type: 'split',
      direction: 'horizontal',
      ratio: 0.55,
      children: [
        {
          type: 'split',
          direction: 'vertical',
          ratio: 0.45,
          children: [
            {
              type: 'split',
              direction: 'horizontal',
              ratio: 0.65,
              children: [
                { type: 'panel', panelId: 'overview' },
                { type: 'panel', panelId: 'legend' },
              ],
            },
            { type: 'tabs', panels: ['detail-view', 'code-editor'], activeIndex: 0 },
          ],
        },
        {
          type: 'split',
          direction: 'vertical',
          ratio: 0.5,
          children: [
            { type: 'panel', panelId: 'node-description' },
            { type: 'tabs', panels: ['node-inspector', 'directory'], activeIndex: 0 },
          ],
        },
      ],
    },
    closedPanels: [],
  },

  'Walkthrough': {
    layout: {
      type: 'split',
      direction: 'horizontal',
      ratio: 0.55,
      children: [
        {
          type: 'split',
          direction: 'vertical',
          ratio: 0.45,
          children: [
            { type: 'panel', panelId: 'overview' },
            { type: 'panel', panelId: 'detail-view' },
          ],
        },
        {
          type: 'split',
          direction: 'vertical',
          ratio: 0.5,
          children: [
            { type: 'panel', panelId: 'node-description' },
            { type: 'panel', panelId: 'preview' },
          ],
        },
      ],
    },
    closedPanels: ['legend', 'code-editor', 'node-inspector', 'directory', 'insights', 'llm-chat', 'shortcuts'],
  },
};

/** Active configuration name (persisted alongside layout state) */
var WM_ACTIVE_CONFIG = 'Default';

/** Backward-compat alias */
const WM_DEFAULT_LAYOUT = WM_LAYOUT_CONFIGS['Default'].layout;

/* ================================================================
   STATE SERIALIZER — persist / restore layout to localStorage
   ================================================================ */
const StateSerializer = {
  STORAGE_KEY: 'wm-layout-state',

  save(layoutRoot, floatingPanels, closedPanels, activeConfig) {
    const state = {
      version: 1,
      layout: this._serializeTree(layoutRoot),
      floating: floatingPanels || [],
      closedPanels: closedPanels || [],
      activeConfig: activeConfig || WM_ACTIVE_CONFIG || 'Default',
      timestamp: Date.now(),
    };
    try { localStorage.setItem(this.STORAGE_KEY, JSON.stringify(state)); }
    catch (_) { /* quota exceeded */ }
  },

  load() {
    try {
      const raw = localStorage.getItem(this.STORAGE_KEY);
      if (!raw) return null;
      const state = JSON.parse(raw);
      return state && state.version === 1 ? state : null;
    } catch (_) { return null; }
  },

  clear() { localStorage.removeItem(this.STORAGE_KEY); },

  _serializeTree(node) {
    if (!node) return null;
    if (node.type === 'panel') return { type: 'panel', panelId: node.panelId };
    if (node.type === 'tabs')  return { type: 'tabs', panels: node.panels.slice(), activeIndex: node.activeIndex };
    if (node.type === 'split') return {
      type: 'split', direction: node.direction, ratio: node.ratio,
      children: [this._serializeTree(node.children[0]), this._serializeTree(node.children[1])],
    };
    return null;
  },
};

/* ================================================================
   PANEL REGISTRY — single source of truth for every panel type
   ================================================================ */
const PanelRegistry = {
  _panels: new Map(),
  _order: [],

  /**
   * Register a panel definition.
   * @param {Object} config
   * @param {string}   config.id         – unique panel id
   * @param {string}   config.title      – display title
   * @param {string}  [config.icon]      – emoji / icon
   * @param {string}  [config.contentId] – id to set on the content div
   * @param {string}  [config.contentClass] – extra CSS class for content div
   * @param {boolean} [config.closable=true]
   * @param {boolean} [config.floatable=true]
   * @param {Function}[config.render]    – fn(contentDiv) called once to populate
   */
  register(config) {
    const entry = Object.assign({
      minWidth: 200, minHeight: 100,
      closable: true, floatable: true, defaultVisible: true,
      strip: false,
    }, config);
    this._panels.set(config.id, entry);
    this._order.push(config.id);
  },

  get(id) { return this._panels.get(id); },
  getAll() {
    const self = this;
    return this._order.map(function (id) { return self._panels.get(id); });
  },
};

/* ================================================================
   LAYOUT ENGINE — binary split-tree → DOM
   ================================================================ */
class LayoutEngine {
  constructor(rootNode) {
    this.root = rootNode
      ? JSON.parse(JSON.stringify(rootNode))
      : JSON.parse(JSON.stringify(WM_DEFAULT_LAYOUT));
    /** @type {Map<string, HTMLElement>} panelId → panel wrapper element */
    this._panelElements = new Map();
  }

  /* ── public ──────────────────────────────────────────────── */

  renderInto(container) {
    // Detach (but keep) existing panel elements
    this._panelElements.forEach(function (el) {
      if (el.parentNode) el.parentNode.removeChild(el);
    });
    container.innerHTML = '';
    if (this.root) this._renderNode(this.root, container);
  }

  removePanel(panelId) {
    this.root = this._removeFromNode(this.root, panelId);
  }

  insertPanel(panelId, targetPanelId, position) {
    this.root = this._insertInNode(this.root, panelId, targetPanelId, position);
  }

  findFirstPanelId(node) {
    if (!node) node = this.root;
    if (!node) return null;
    if (node.type === 'panel') return node.panelId;
    if (node.type === 'tabs')  return node.panels[0] || null;
    if (node.type === 'split') return this.findFirstPanelId(node.children[0]);
    return null;
  }

  hasPanelInTree(panelId, node) {
    if (node === undefined) node = this.root;
    if (!node) return false;
    if (node.type === 'panel') return node.panelId === panelId;
    if (node.type === 'tabs')  return node.panels.indexOf(panelId) !== -1;
    if (node.type === 'split') return this.hasPanelInTree(panelId, node.children[0])
                                      || this.hasPanelInTree(panelId, node.children[1]);
    return false;
  }

  /** If panelId lives inside a tab group, set it as the active tab. Returns true if switched. */
  activateTab(panelId, node) {
    if (node === undefined) node = this.root;
    if (!node) return false;
    if (node.type === 'tabs') {
      var idx = node.panels.indexOf(panelId);
      if (idx !== -1) { node.activeIndex = idx; return true; }
    }
    if (node.type === 'split') {
      return this.activateTab(panelId, node.children[0])
          || this.activateTab(panelId, node.children[1]);
    }
    return false;
  }

  destroyPanel(panelId) {
    var el = this._panelElements.get(panelId);
    if (el && el.parentNode) el.parentNode.removeChild(el);
    this._panelElements.delete(panelId);
  }

  /* ── private: tree → DOM ─────────────────────────────────── */

  _renderNode(node, parent) {
    if (!node) return;

    if (node.type === 'panel') {
      var el = this._panelElements.get(node.panelId);
      if (!el) {
        el = this._createPanelElement(node.panelId);
        if (!el) return;
        this._panelElements.set(node.panelId, el);
      }
      // ensure title-bar visible (may have been hidden in tab mode)
      var tb = el.querySelector('.wm-panel-titlebar');
      if (tb) tb.style.display = '';
      parent.appendChild(el);
      return;
    }

    if (node.type === 'tabs') {
      this._renderTabGroup(node, parent);
      return;
    }

    if (node.type === 'split') {
      var wrapper = document.createElement('div');
      wrapper.className = 'wm-split wm-split-' + node.direction;

      var first = document.createElement('div');
      first.className = 'wm-split-pane';
      first.style.flexBasis = (node.ratio * 100) + '%';
      first.style.flexGrow = '0';
      first.style.flexShrink = '0';

      var splitter = this._createSplitter(node, first, wrapper);

      var second = document.createElement('div');
      second.className = 'wm-split-pane wm-split-second';

      wrapper.appendChild(first);
      wrapper.appendChild(splitter);
      wrapper.appendChild(second);
      parent.appendChild(wrapper);

      this._renderNode(node.children[0], first);
      this._renderNode(node.children[1], second);
    }
  }


  _createPanelElement(panelId) {
    var config = PanelRegistry.get(panelId);
    if (!config) return null;

    var panel = document.createElement('div');
    panel.className = 'wm-panel';
    panel.dataset.panelId = panelId;

    // ── title bar ──
    var titleBar = document.createElement('div');
    titleBar.className = 'wm-panel-titlebar';
    titleBar.dataset.panelId = panelId;

    var left = document.createElement('div');
    left.className = 'wm-panel-titlebar-left';
    left.innerHTML = '<span class="wm-panel-icon">' + (config.icon || '') +
      '</span><span class="wm-panel-title">' + (config.title || panelId) + '</span>';

    var actions = document.createElement('div');
    actions.className = 'wm-panel-actions';

    if (config.floatable !== false) {
      var pb = document.createElement('button');
      pb.className = 'wm-btn-popout'; pb.title = 'Pop out'; pb.textContent = '⧉';
      pb.addEventListener('click', function (e) { e.stopPropagation(); WindowManager.floatPanel(panelId); });
      actions.appendChild(pb);
    }
    if (config.closable !== false) {
      var cb = document.createElement('button');
      cb.className = 'wm-btn-close'; cb.title = 'Close'; cb.textContent = '✕';
      cb.addEventListener('click', function (e) { e.stopPropagation(); WindowManager.closePanel(panelId); });
      actions.appendChild(cb);
    }

    titleBar.appendChild(left);
    titleBar.appendChild(actions);

    // ── content ──
    var content = document.createElement('div');
    content.className = 'wm-panel-content';
    if (config.contentId)    content.id = config.contentId;
    if (config.contentClass) content.classList.add(config.contentClass);

    panel.appendChild(titleBar);
    panel.appendChild(content);

    if (config.render) config.render(content);
    return panel;
  }

  _renderTabGroup(tabNode, parent) {
    var container = document.createElement('div');
    container.className = 'wm-tab-container';

    var tabBar = document.createElement('div');
    tabBar.className = 'wm-tab-bar';

    var contentArea = document.createElement('div');
    contentArea.className = 'wm-tab-content-area';

    var self = this;

    tabNode.panels.forEach(function (panelId, idx) {
      var config = PanelRegistry.get(panelId);
      var tab = document.createElement('div');
      tab.className = 'wm-tab' + (idx === tabNode.activeIndex ? ' active' : '');
      tab.dataset.panelId = panelId;

      var label = document.createElement('span');
      label.textContent = (config ? config.icon || '' : '') + ' ' + (config ? config.title : panelId);
      tab.appendChild(label);

      if (config && config.closable !== false) {
        var x = document.createElement('span');
        x.className = 'wm-tab-close'; x.textContent = '✕';
        x.addEventListener('click', function (e) { e.stopPropagation(); WindowManager.closePanel(panelId); });
        tab.appendChild(x);
      }

      tab.addEventListener('click', function () {
        tabNode.activeIndex = idx;
        WindowManager.rebuildLayout();
        WindowManager._saveState();
      });
      tabBar.appendChild(tab);
    });

    container.appendChild(tabBar);
    container.appendChild(contentArea);
    parent.appendChild(container);

    var activeId = tabNode.panels[tabNode.activeIndex];
    if (activeId) {
      var el = this._panelElements.get(activeId);
      if (!el) {
        el = this._createPanelElement(activeId);
        if (el) this._panelElements.set(activeId, el);
      }
      if (el) {
        var tb = el.querySelector('.wm-panel-titlebar');
        if (tb) tb.style.display = 'none';
        contentArea.appendChild(el);
      }
    }
  }

  _createSplitter(splitNode, firstPane, wrapper) {
    var isHoriz = splitNode.direction === 'horizontal';
    var splitter = document.createElement('div');
    splitter.className = 'wm-splitter wm-splitter-' + splitNode.direction;

    splitter.addEventListener('mousedown', function (e) {
      if (e.button !== 0) return;
      e.preventDefault();
      var parentRect = wrapper.getBoundingClientRect();
      document.body.classList.add('wm-resizing');

      function onMove(ev) {
        var pos = isHoriz
          ? (ev.clientX - parentRect.left) / parentRect.width
          : (ev.clientY - parentRect.top) / parentRect.height;
        var clamped = Math.max(0.05, Math.min(0.95, pos));
        splitNode.ratio = clamped;
        firstPane.style.flexBasis = (clamped * 100) + '%';
        window.dispatchEvent(new CustomEvent('wm-panel-resize'));
      }
      function onUp() {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        document.body.classList.remove('wm-resizing');
        document.body.classList.remove('wm-resizing-horizontal');
        document.body.classList.remove('wm-resizing-vertical');
        WindowManager._saveState();
        WindowManager._triggerRerender();
      }
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
    return splitter;
  }

  /* ── private: tree mutation ──────────────────────────────── */

  _removeFromNode(node, panelId) {
    if (!node) return null;
    if (node.type === 'panel') return node.panelId === panelId ? null : node;
    if (node.type === 'tabs') {
      node.panels = node.panels.filter(function (id) { return id !== panelId; });
      if (node.panels.length === 0) return null;
      if (node.panels.length === 1) return { type: 'panel', panelId: node.panels[0] };
      if (node.activeIndex >= node.panels.length) node.activeIndex = node.panels.length - 1;
      return node;
    }
    if (node.type === 'split') {
      node.children[0] = this._removeFromNode(node.children[0], panelId);
      node.children[1] = this._removeFromNode(node.children[1], panelId);
      if (!node.children[0]) return node.children[1];
      if (!node.children[1]) return node.children[0];
      return node;
    }
    return node;
  }

  _insertInNode(node, panelId, targetId, position) {
    if (!node) return node;
    if (node.type === 'panel' && node.panelId === targetId) {
      if (position === 'tab') {
        return { type: 'tabs', panels: [targetId, panelId], activeIndex: 1 };
      }
      var newPanel = { type: 'panel', panelId: panelId };
      var dir = (position === 'left' || position === 'right') ? 'horizontal' : 'vertical';
      var first = (position === 'left' || position === 'top');
      // Strip panels get a tiny ratio so they appear as a thin strip
      var panelConfig = PanelRegistry.get(panelId);
      var ratio = 0.5;
      if (panelConfig && panelConfig.strip) {
        var stripRatio = 0.08;
        ratio = first ? stripRatio : (1 - stripRatio);
      }
      return {
        type: 'split', direction: dir, ratio: ratio,
        children: first ? [newPanel, node] : [node, newPanel],
      };
    }
    if (node.type === 'tabs') {
      if (node.panels.indexOf(targetId) !== -1 && position === 'tab') {
        node.panels.push(panelId);
        node.activeIndex = node.panels.length - 1;
        return node;
      }
    }
    if (node.type === 'split') {
      node.children[0] = this._insertInNode(node.children[0], panelId, targetId, position);
      node.children[1] = this._insertInNode(node.children[1], panelId, targetId, position);
    }
    return node;
  }
}

/* ================================================================
   DOCK MANAGER — drag title-bar to rearrange panels
   ================================================================ */
class DockManager {
  constructor(layoutEngine) {
    this.layout = layoutEngine;
    this._overlay = null;
    this._ghostEl = null;
  }

  initDrag(panelId, titleBarEl) {
    if (titleBarEl._wmDockWired) return;
    titleBarEl._wmDockWired = true;

    var self = this;
    var startX, startY, isDragging;

    titleBarEl.addEventListener('mousedown', function (e) {
      if (e.button !== 0 || e.target.closest('.wm-panel-actions')) return;
      startX = e.clientX; startY = e.clientY; isDragging = false;

      function onMove(ev) {
        if (!isDragging && Math.abs(ev.clientX - startX) + Math.abs(ev.clientY - startY) > 8) {
          isDragging = true;
          self._ghostEl = self._createGhost(panelId, ev);
          document.body.classList.add('wm-docking');
        }
        if (isDragging && self._ghostEl) {
          self._ghostEl.style.left = (ev.clientX - 60) + 'px';
          self._ghostEl.style.top  = (ev.clientY - 14) + 'px';
          self._showDropHighlight(self._findDropTarget(ev.clientX, ev.clientY, panelId));
        }
      }
      function onUp(ev) {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        document.body.classList.remove('wm-docking');
        if (self._ghostEl) { self._ghostEl.remove(); self._ghostEl = null; }
        self._clearDropHighlight();
        if (isDragging) {
          var drop = self._findDropTarget(ev.clientX, ev.clientY, panelId);
          if (drop) {
            self.layout.removePanel(panelId);
            self.layout.insertPanel(panelId, drop.targetPanelId, drop.position);
            WindowManager.rebuildLayout();
            WindowManager._saveState();
            WindowManager._triggerRerender();
          }
        }
      }
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }

  _createGhost(panelId, ev) {
    var config = PanelRegistry.get(panelId);
    var ghost = document.createElement('div');
    ghost.className = 'wm-dock-ghost';
    ghost.textContent = (config ? (config.icon || '') + ' ' + config.title : panelId);
    ghost.style.left = (ev.clientX - 60) + 'px';
    ghost.style.top  = (ev.clientY - 14) + 'px';
    document.body.appendChild(ghost);
    return ghost;
  }

  _findDropTarget(x, y, excludeId) {
    var panels = document.querySelectorAll('.wm-panel[data-panel-id]');
    for (var i = 0; i < panels.length; i++) {
      var p = panels[i];
      var pid = p.dataset.panelId;
      if (pid === excludeId) continue;
      var r = p.getBoundingClientRect();
      if (x < r.left || x > r.right || y < r.top || y > r.bottom) continue;
      var rx = (x - r.left) / r.width, ry = (y - r.top) / r.height;
      var pos;
      if      (ry < 0.22) pos = 'top';
      else if (ry > 0.78) pos = 'bottom';
      else if (rx < 0.22) pos = 'left';
      else if (rx > 0.78) pos = 'right';
      else                pos = 'tab';
      return { targetPanelId: pid, position: pos };
    }
    return null;
  }

  _showDropHighlight(target) {
    this._clearDropHighlight();
    if (!target) return;
    var panel = document.querySelector('.wm-panel[data-panel-id="' + target.targetPanelId + '"]');
    if (!panel) return;
    var overlay = document.createElement('div');
    overlay.className = 'wm-drop-overlay';
    var css = { top:'top:0;left:0;right:0;height:50%;', bottom:'bottom:0;left:0;right:0;height:50%;',
                left:'top:0;left:0;bottom:0;width:50%;', right:'top:0;right:0;bottom:0;width:50%;',
                tab:'top:0;left:0;right:0;bottom:0;' };
    overlay.style.cssText = 'position:absolute;' + (css[target.position] || '') +
      'background:rgba(88,166,255,0.12);border:2px dashed var(--accent);z-index:9000;pointer-events:none;border-radius:4px;';
    panel.appendChild(overlay);
    this._overlay = overlay;
  }

  _clearDropHighlight() {
    if (this._overlay) { this._overlay.remove(); this._overlay = null; }
  }
}

/* ================================================================
   FLOATING MANAGER — pop-out panels into draggable overlays
   ================================================================ */
class FloatingManager {
  constructor() {
    /** @type {Map<string, HTMLElement>} panelId → floating wrapper */
    this.floatingPanels = new Map();
  }

  popOut(panelId, layoutEngine) {
    var config = PanelRegistry.get(panelId);
    if (!config) return;

    var existingEl = layoutEngine._panelElements.get(panelId);

    var floater = document.createElement('div');
    floater.className = 'wm-floating-panel';
    floater.dataset.panelId = panelId;

    // title bar
    var titleBar = document.createElement('div');
    titleBar.className = 'wm-panel-titlebar wm-floating-titlebar';
    titleBar.innerHTML =
      '<div class="wm-panel-titlebar-left"><span class="wm-panel-icon">' + (config.icon || '') +
      '</span><span class="wm-panel-title">' + (config.title || panelId) + '</span></div>' +
      '<div class="wm-panel-actions"></div>';

    var actions = titleBar.querySelector('.wm-panel-actions');
    var dockBtn = document.createElement('button');
    dockBtn.className = 'wm-btn-dock'; dockBtn.title = 'Dock back'; dockBtn.textContent = '⇲';
    dockBtn.addEventListener('click', function () { WindowManager.dockPanel(panelId); });
    var closeBtn = document.createElement('button');
    closeBtn.className = 'wm-btn-close'; closeBtn.title = 'Close'; closeBtn.textContent = '✕';
    closeBtn.addEventListener('click', function () {
      WindowManager.floating.close(panelId);
      WindowManager._saveState();
      WindowManager._updateViewMenu();
    });
    actions.appendChild(dockBtn);
    actions.appendChild(closeBtn);

    var content = document.createElement('div');
    content.className = 'wm-panel-content wm-floating-content';
    if (config.contentId)    content.id = config.contentId;
    if (config.contentClass) content.classList.add(config.contentClass);

    floater.appendChild(titleBar);
    floater.appendChild(content);

    // Transfer existing panel content if available
    if (existingEl) {
      var oldContent = existingEl.querySelector('.wm-panel-content');
      if (oldContent) {
        while (oldContent.firstChild) content.appendChild(oldContent.firstChild);
        if (oldContent.id) content.id = oldContent.id;
      }
      layoutEngine._panelElements.delete(panelId);
      if (existingEl.parentNode) existingEl.parentNode.removeChild(existingEl);
    } else {
      if (config.render) config.render(content);
    }

    document.body.appendChild(floater);
    this._makeDraggable(floater, titleBar);
    this.floatingPanels.set(panelId, floater);
  }

  close(panelId) {
    var f = this.floatingPanels.get(panelId);
    if (f) { f.remove(); this.floatingPanels.delete(panelId); }
  }

  dockBack(panelId, layoutEngine) {
    var f = this.floatingPanels.get(panelId);
    if (!f) return;
    f.remove();
    this.floatingPanels.delete(panelId);
    layoutEngine._panelElements.delete(panelId);   // will be recreated
  }

  _makeDraggable(el, handle) {
    var ox, oy;
    handle.addEventListener('mousedown', function (e) {
      if (e.button !== 0 || e.target.closest('.wm-panel-actions')) return;
      e.preventDefault();
      ox = e.clientX - el.offsetLeft;
      oy = e.clientY - el.offsetTop;
      function onMove(ev) { el.style.left = (ev.clientX - ox) + 'px'; el.style.top = (ev.clientY - oy) + 'px'; }
      function onUp() {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        WindowManager._saveState();
      }
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }

  getState() {
    var result = [];
    this.floatingPanels.forEach(function (el, id) {
      result.push({ panelId: id, x: el.offsetLeft, y: el.offsetTop, w: el.offsetWidth, h: el.offsetHeight });
    });
    return result;
  }
}

/* ================================================================
   WINDOW MANAGER — top-level orchestrator (singleton)
   ================================================================ */
var WindowManager = {
  /** @type {LayoutEngine} */  layout: null,
  /** @type {DockManager} */   dock: null,
  /** @type {FloatingManager}*/floating: null,
  /** @type {Set<string>} */   closedPanels: new Set(),
  /** @type {HTMLElement} */   container: null,
  _resizeTimeout: null,

  /* ── lifecycle ───────────────────────────────────────────── */

  init: function (containerEl) {
    this.container = containerEl;
    var saved = StateSerializer.load();

    var layoutRoot = null;
    if (saved && saved.layout && this._validateLayout(saved.layout)) {
      layoutRoot = saved.layout;
    }
    if (saved && saved.activeConfig) {
      WM_ACTIVE_CONFIG = saved.activeConfig;
    }

    this.layout   = new LayoutEngine(layoutRoot || WM_DEFAULT_LAYOUT);
    this.floating = new FloatingManager();
    this.dock     = new DockManager(this.layout);
    this.closedPanels = new Set(saved ? saved.closedPanels || [] : []);

    this.rebuildLayout();

    // Restore floating panels
    var self = this;
    if (saved && saved.floating) {
      saved.floating.forEach(function (f) {
        self.floating.popOut(f.panelId, self.layout);
        var el = self.floating.floatingPanels.get(f.panelId);
        if (el) {
          el.style.left  = f.x + 'px'; el.style.top    = f.y + 'px';
          el.style.width = f.w + 'px'; el.style.height = f.h + 'px';
        }
      });
    }

    this._setupViewMenu();
    this._setupConfigMenu();
    this._updateViewMenu();
    this._updateConfigMenu();
    window.addEventListener('beforeunload', function () { self._saveState(); });
  },

  rebuildLayout: function () {
    if (!this.container || !this.layout) return;
    this.layout.renderInto(this.container);
    this._wireUpDocking();
    this._applyStripPanes();
    window.dispatchEvent(new CustomEvent('wm-panel-resize'));
  },

  /* ── panel operations ────────────────────────────────────── */

  closePanel: function (panelId) {
    this.closedPanels.add(panelId);
    this.layout.removePanel(panelId);
    this.layout.destroyPanel(panelId);
    this.rebuildLayout();
    this._saveState();
    this._updateViewMenu();
    this._triggerRerender();
  },

  openPanel: function (panelId) {
    this.closedPanels.delete(panelId);
    var target = this.layout.findFirstPanelId(this.layout.root);
    var config = PanelRegistry.get(panelId);
    var position = (config && config.strip) ? 'bottom' : 'right';
    if (target) this.layout.insertPanel(panelId, target, position);
    else        this.layout.root = { type: 'panel', panelId: panelId };
    this.rebuildLayout();
    this._saveState();
    this._updateViewMenu();
    this._triggerRerender();
  },

  floatPanel: function (panelId) {
    this.layout.removePanel(panelId);
    this.rebuildLayout();
    this.floating.popOut(panelId, this.layout);
    this._saveState();
    this._updateViewMenu();
    this._triggerRerender();
  },

  dockPanel: function (panelId) {
    this.floating.dockBack(panelId, this.layout);
    this.closedPanels.delete(panelId);
    var target = this.layout.findFirstPanelId(this.layout.root);
    var config = PanelRegistry.get(panelId);
    var position = (config && config.strip) ? 'bottom' : 'right';
    if (target) this.layout.insertPanel(panelId, target, position);
    else        this.layout.root = { type: 'panel', panelId: panelId };
    this.rebuildLayout();
    this._saveState();
    this._updateViewMenu();
    this._triggerRerender();
  },

  resetLayout: function () {
    this.floating.floatingPanels.forEach(function (el) { el.remove(); });
    this.floating.floatingPanels.clear();
    this.layout._panelElements.forEach(function (el) { if (el.parentNode) el.parentNode.removeChild(el); });
    this.layout._panelElements.clear();
    this.closedPanels.clear();
    var cfg = WM_LAYOUT_CONFIGS[WM_ACTIVE_CONFIG] || WM_LAYOUT_CONFIGS['Default'];
    this.layout.root = JSON.parse(JSON.stringify(cfg.layout));
    if (cfg.closedPanels) {
      var self = this;
      cfg.closedPanels.forEach(function(pid) { self.closedPanels.add(pid); });
    }
    StateSerializer.clear();
    this.rebuildLayout();
    this._updateViewMenu();
    this._updateConfigMenu();
    this._triggerRerender();
  },

  /** Switch to a named layout configuration */
  applyConfig: function (configName) {
    var cfg = WM_LAYOUT_CONFIGS[configName];
    if (!cfg) return;

    WM_ACTIVE_CONFIG = configName;

    // Tear down everything
    this.floating.floatingPanels.forEach(function (el) { el.remove(); });
    this.floating.floatingPanels.clear();
    this.layout._panelElements.forEach(function (el) { if (el.parentNode) el.parentNode.removeChild(el); });
    this.layout._panelElements.clear();
    this.closedPanels.clear();

    // Apply new layout tree
    this.layout.root = JSON.parse(JSON.stringify(cfg.layout));
    if (cfg.closedPanels) {
      var self = this;
      cfg.closedPanels.forEach(function(pid) { self.closedPanels.add(pid); });
    }

    this.rebuildLayout();
    this._saveState();
    this._updateViewMenu();
    this._updateConfigMenu();
    this._triggerRerender();
  },

  /* ── internal helpers ────────────────────────────────────── */

  _wireUpDocking: function () {
    if (!this.dock || !this.container) return;
    var self = this;
    this.container.querySelectorAll('.wm-panel-titlebar[data-panel-id]').forEach(function (tb) {
      var pid = tb.dataset.panelId;
      if (pid) self.dock.initDrag(pid, tb);
    });
  },

  /**
   * Post-render DOM walk: find every strip panel and force its containing
   * split-pane to auto-size.  Works regardless of tree nesting (tabs, deep
   * splits, restored layouts, drag-and-drop, etc.).
   */
  _applyStripPanes: function () {
    var container = this.container;
    if (!container) return;
    var allConfigs = PanelRegistry.getAll();
    for (var i = 0; i < allConfigs.length; i++) {
      var cfg = allConfigs[i];
      if (!cfg.strip) continue;

      var panelEl = container.querySelector('.wm-panel[data-panel-id="' + cfg.id + '"]');
      if (!panelEl) continue;

      // Walk up to the nearest split-pane wrapper
      var pane = panelEl.closest('.wm-split-pane');
      if (!pane) continue;

      // Mark the strip pane (CSS handles the sizing via !important)
      pane.classList.add('wm-split-pane-strip');
      // Clear any inline flex that _renderNode set — let CSS !important take over
      pane.style.flexBasis = '';
      pane.style.flexGrow = '';
      pane.style.flexShrink = '';
      pane.style.flex = '';

      // Find the parent split wrapper
      var splitWrapper = pane.parentElement;
      if (!splitWrapper || !splitWrapper.classList.contains('wm-split')) continue;

      // Walk siblings: hide the splitter, make the other pane fill remaining space
      var children = splitWrapper.children;
      for (var j = 0; j < children.length; j++) {
        var child = children[j];
        if (child === pane) continue;
        if (child.classList.contains('wm-splitter')) {
          child.classList.add('wm-splitter-strip');
        } else if (child.classList.contains('wm-split-pane')) {
          // Sibling pane takes all remaining space
          child.style.flex = '1 1 auto';
          child.style.flexBasis = '';
        }
      }
    }
  },

  _saveState: function () {
    if (!this.layout) return;
    StateSerializer.save(this.layout.root, this.floating.getState(), Array.from(this.closedPanels), WM_ACTIVE_CONFIG);
  },

  _triggerRerender: function () {
    if (this._resizeTimeout) clearTimeout(this._resizeTimeout);
    this._resizeTimeout = setTimeout(function () {
      if (typeof renderGraph === 'function' && typeof graphData !== 'undefined' && graphData) {
        renderGraph();
      }
    }, 120);
  },

  _validateLayout: function (node) {
    if (!node) return false;
    if (node.type === 'panel') return PanelRegistry.get(node.panelId) != null;
    if (node.type === 'tabs')  return node.panels && node.panels.every(function (id) { return PanelRegistry.get(id) != null; });
    if (node.type === 'split') return node.children && node.children.length === 2
      && this._validateLayout(node.children[0]) && this._validateLayout(node.children[1]);
    return false;
  },

  _setupViewMenu: function () {
    var btn  = document.getElementById('wm-view-btn');
    var menu = document.getElementById('wm-view-menu');
    var resetBtn = document.getElementById('wm-reset-btn');
    if (!btn || !menu) return;

    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var cfgMenu = document.getElementById('wm-config-menu');
      if (cfgMenu) cfgMenu.classList.remove('open');
      menu.classList.toggle('open');
    });
    document.addEventListener('click', function () {
      menu.classList.remove('open');
      var cfgMenu = document.getElementById('wm-config-menu');
      if (cfgMenu) cfgMenu.classList.remove('open');
    });
    menu.addEventListener('click', function (e) { e.stopPropagation(); });
    if (resetBtn) resetBtn.addEventListener('click', function () { WindowManager.resetLayout(); });
  },

  _updateViewMenu: function () {
    var menu = document.getElementById('wm-view-menu');
    if (!menu) return;
    var self = this;
    var html = '';
    PanelRegistry.getAll().forEach(function (p) {
      var inLayout  = self.layout.hasPanelInTree(p.id);
      var isFloat   = self.floating.floatingPanels.has(p.id);
      var isOpen    = inLayout || isFloat;
      html += '<div class="wm-view-item' + (isOpen ? ' active' : '') + '" data-panel-id="' + p.id + '">' +
        '<span class="wm-view-check">' + (isOpen ? '✓' : '') + '</span>' +
        '<span>' + (p.icon || '') + ' ' + p.title + (isFloat ? ' (floating)' : '') + '</span></div>';
    });
    menu.innerHTML = html;
    menu.querySelectorAll('.wm-view-item').forEach(function (item) {
      item.addEventListener('click', function () {
        var pid = item.dataset.panelId;
        var inL = self.layout.hasPanelInTree(pid);
        var isF = self.floating.floatingPanels.has(pid);
        if (isF)      self.dockPanel(pid);
        else if (inL) self.closePanel(pid);
        else          self.openPanel(pid);
      });
    });
  },

  /* ── Layout Configuration Menu ─────────────────────────── */

  _setupConfigMenu: function () {
    var btn  = document.getElementById('wm-config-btn');
    var menu = document.getElementById('wm-config-menu');
    if (!btn || !menu) return;

    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var viewMenu = document.getElementById('wm-view-menu');
      if (viewMenu) viewMenu.classList.remove('open');
      menu.classList.toggle('open');
    });
    menu.addEventListener('click', function (e) { e.stopPropagation(); });
  },

  _updateConfigMenu: function () {
    var menu = document.getElementById('wm-config-menu');
    var btn  = document.getElementById('wm-config-btn');
    if (!menu) return;

    var self = this;
    var html = '';
    var configNames = Object.keys(WM_LAYOUT_CONFIGS);
    configNames.forEach(function (name) {
      var isActive = (name === WM_ACTIVE_CONFIG);
      html += '<div class="wm-view-item' + (isActive ? ' active' : '') + '" data-config="' + name + '">' +
        '<span class="wm-view-check">' + (isActive ? '◆' : '') + '</span>' +
        '<span>' + name + '</span></div>';
    });
    menu.innerHTML = html;

    // Update button label
    if (btn) btn.textContent = '◈ ' + WM_ACTIVE_CONFIG + ' ▾';

    menu.querySelectorAll('.wm-view-item').forEach(function (item) {
      item.addEventListener('click', function () {
        var configName = item.dataset.config;
        if (configName && configName !== WM_ACTIVE_CONFIG) {
          self.applyConfig(configName);
          menu.classList.remove('open');
        }
      });
    });
  },
};

