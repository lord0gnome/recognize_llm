/**
 * Shows the description Recognize LLM generated for an image.
 *
 *  - Files: a "Description" tab in the file sidebar, registered in the @nextcloud/files tab
 *    registry (window._nc_files_scope.v4_0.filesSidebarTabs — the same Map that the Files app
 *    and Memories read through getSidebarTabs()).
 *  - Memories: a block at the top of the Info panel, in both of Memories' sidebars (its own
 *    "reduced" one and the native-tab shell it uses for the user's own files). Done by wrapping
 *    the global _m.sidebar.open(), which the viewer calls for every photo it shows. The tab
 *    itself is hidden in Memories so the description isn't shown twice.
 *
 * Plain script, no build step. Loaded via the LoadSidebar event (Files and Memories pages).
 */
(function () {
	'use strict'

	const APP = 'recognize_llm_settings'
	const TAB_ID = 'recognize_llm-description'
	const TAG = 'recognize-llm-description-tab'
	const BLOCK_CLASS = 'rllm-description-block'
	const ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M14 17H7v-2h7m3-2H7v-2h10m0-2H7V7h10m2-4H5c-1.11 0-2 .89-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2z"/></svg>'

	const tr = (s) => (window.t ? window.t(APP, s) : s)

	// ── Data ────────────────────────────────────────────────────────────────────────────────────
	const cache = new Map() // fileId -> Promise<string|null>

	function fetchDescription(fileId) {
		fileId = Number(fileId)
		if (!fileId) return Promise.resolve(null)
		if (cache.has(fileId)) return cache.get(fileId)
		const root = (window.OC && window.OC.webroot) || ''
		const p = fetch(`${root}/ocs/v2.php/apps/${APP}/api/v1/description/${fileId}?format=json`, {
			headers: {
				'OCS-APIRequest': 'true',
				Accept: 'application/json',
				requesttoken: (window.OC && window.OC.requestToken) || '',
			},
			credentials: 'same-origin',
		}).then(async (r) => {
			if (!r.ok) return null
			const body = await r.json()
			return body?.ocs?.data?.description ?? null
		}).catch(() => {
			cache.delete(fileId) // network blip: allow a retry next time
			return null
		})
		cache.set(fileId, p)
		if (cache.size > 500) cache.delete(cache.keys().next().value)
		return p
	}

	function renderInto(el, description) {
		el.replaceChildren()
		const text = document.createElement('p')
		text.className = 'rllm-description-text'
		if (description) {
			text.textContent = description
		} else {
			text.classList.add('rllm-description-empty')
			text.textContent = tr('No description yet. Recognize LLM adds one once it has processed this file.')
		}
		el.append(text)
	}

	// ── Files: sidebar tab ──────────────────────────────────────────────────────────────────────
	class DescriptionTab extends HTMLElement {
		set node(node) {
			this._node = node
			this.load()
		}

		get node() {
			return this._node
		}

		set active(v) {
			this._active = v
		}

		connectedCallback() {
			this.classList.add('rllm-description-tab')
			this.load()
		}

		async load() {
			const fileId = this._node?.fileid ?? this._node?.id
			if (!this.isConnected || !fileId) return
			const token = (this._token = {})
			this.replaceChildren()
			const loading = document.createElement('p')
			loading.className = 'rllm-description-empty'
			loading.textContent = tr('Loading…')
			this.append(loading)
			const description = await fetchDescription(fileId)
			if (token === this._token) renderInto(this, description)
		}
	}

	function registerTab() {
		const scope = ((window._nc_files_scope ??= {}).v4_0 ??= {})
		const tabs = (scope.filesSidebarTabs ??= new Map())
		if (tabs.has(TAB_ID)) return
		tabs.set(TAB_ID, {
			id: TAB_ID,
			order: 90,
			displayName: tr('Description'),
			iconSvgInline: ICON,
			tagName: TAG,
			enabled: (ctx) => ctx?.view?.id !== 'memories' && ctx?.node?.type === 'file',
			async onInit() {
				if (!window.customElements.get(TAG)) window.customElements.define(TAG, DescriptionTab)
			},
		})
	}

	// ── Memories: block at the top of the Info panel ────────────────────────────────────────────
	let openSeq = 0

	function findInfoAnchor() {
		// Memories' own ("reduced") sidebar: put it under the title/size header.
		const reducedHeader = document.querySelector('#app-sidebar-vue .top-info')
		if (reducedHeader) return { parent: reducedHeader.parentNode, before: reducedHeader.nextSibling }
		// Native shell: before the Info tab's custom element, inside that tab's section.
		const metadata = document.querySelector('#app-sidebar-native memories-files-sidebar-tab')
		if (metadata) return { parent: metadata.parentNode, before: metadata }
		return null
	}

	async function showInMemories(photo) {
		const seq = ++openSeq
		const fileId = typeof photo === 'number' ? photo : photo?.fileid
		document.querySelectorAll('.' + BLOCK_CLASS).forEach((el) => el.remove())
		if (!fileId) return
		const description = fetchDescription(fileId)

		// The panel content renders after open() resolves (the native tab inits lazily): wait for it.
		let anchor = null
		for (let i = 0; i < 40 && seq === openSeq; i++) {
			anchor = findInfoAnchor()
			if (anchor) break
			await new Promise((resolve) => setTimeout(resolve, 100))
		}
		if (!anchor || seq !== openSeq) return

		const block = document.createElement('section')
		block.className = BLOCK_CLASS
		const title = document.createElement('h3')
		title.textContent = tr('Description')
		block.append(title)
		const body = document.createElement('div')
		block.append(body)
		renderInto(body, null)
		body.firstChild.textContent = tr('Loading…')
		anchor.parent.insertBefore(block, anchor.before)

		const text = await description
		if (seq === openSeq && block.isConnected) renderInto(body, text)
	}

	function hookMemories() {
		const sidebar = window._m?.sidebar
		if (!sidebar || typeof sidebar.open !== 'function' || sidebar.__rllmHooked) return
		const open = sidebar.open
		sidebar.open = async function (photo, ...rest) {
			const result = await open.call(this, photo, ...rest)
			showInMemories(photo).catch(() => {})
			return result
		}
		const close = sidebar.close
		if (typeof close === 'function') {
			sidebar.close = async function (...args) {
				openSeq++
				document.querySelectorAll('.' + BLOCK_CLASS).forEach((el) => el.remove())
				return close.apply(this, args)
			}
		}
		sidebar.__rllmHooked = true
	}

	registerTab()
	if (document.querySelector('#content.app-memories, #content-vue.app-memories') || /\/apps\/memories(\/|$)/.test(location.pathname)) {
		// Memories (re)creates _m.sidebar when its Sidebar component mounts: keep checking.
		hookMemories()
		setInterval(hookMemories, 1000)
	}
})()
