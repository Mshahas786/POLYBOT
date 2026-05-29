import './style.css'
import * as api from './api.js'
import {
  render,
  fetchTrades,
  fetchLogs,
  fetchBayesian,
  loadConfig,
  setOffline,
  toast,
} from './dashboard.js'

// ── State ──
let refreshInt = 2000
let timer
let logTimer
let logPolling = false
let activeTab = 'main'

const $ = (id) => document.getElementById(id)

// ── Sync mobile status to desktop sidebar ──
function syncDesktopStatus() {
  const dot = $('statusDotEl')
  if (dot) {
    const desktop = $('statusDotEl-desktop')
    if (desktop) desktop.innerHTML = dot.innerHTML
  }
  const state = $('engineState')
  if (state) {
    const desktop = $('engineState-desktop')
    if (desktop) {
      desktop.textContent = state.textContent
      desktop.style.color = state.style.color
    }
  }
  const badge = $('modeBadge')
  if (badge) {
    const desktop = $('modeBadge-desktop')
    if (desktop) {
      desktop.textContent = badge.textContent
      desktop.className = badge.className
    }
  }
}

// ── Tab Switching ──
function switchTab(tabName) {
  // Update sidebar tabs
  document.querySelectorAll('.sidebar-tab').forEach((b) => {
    b.classList.toggle('tab-active', b.dataset.tab === tabName)
  })
  // Update bottom tabs
  document.querySelectorAll('.bottom-tab').forEach((b) => {
    b.classList.toggle('tab-active', b.dataset.tab === tabName)
  })
  // Show/hide panels
  document.querySelectorAll('.tab-panel').forEach((p) => p.classList.add('hidden'))
  const panel = $('tab-' + tabName)
  if (panel) panel.classList.remove('hidden')
  activeTab = tabName

  if (activeTab === 'logs') {
    fetchLogs()
    startLogPoll()
  } else {
    stopLogPoll()
  }
  if (activeTab === 'trades') fetchTrades(window._tradeFilter || 'all')
  if (activeTab === 'bayesian') fetchBayesian()
}

document.querySelectorAll('.sidebar-tab, .bottom-tab').forEach((btn) => {
  btn.addEventListener('click', () => switchTab(btn.dataset.tab))
})

// ── Data Fetching ──
async function fetchAll() {
  try {
    const data = await api.getStatus()
    render(data)
  } catch (e) {
    setOffline(e.message)
  }
  syncDesktopStatus()
}

function startLogPoll() {
  if (logPolling) return
  logPolling = true
  logTimer = setInterval(fetchLogs, 3000)
}

function stopLogPoll() {
  logPolling = false
  if (logTimer) {
    clearInterval(logTimer)
    logTimer = null
  }
}

// ── Init ──
fetchAll()
timer = setInterval(fetchAll, refreshInt)
loadConfig()

addEventListener('refresh', fetchAll)
addEventListener('settings-saved', fetchAll)

// ── Adj (+/-) Config Stepper ──
window.adj = function (id, delta) {
  const el = document.getElementById(id)
  if (!el) return
  const min = el.hasAttribute('min') ? parseFloat(el.getAttribute('min')) : -Infinity
  const max = el.hasAttribute('max') ? parseFloat(el.getAttribute('max')) : Infinity
  const step = parseFloat(el.getAttribute('step')) || 1
  let val = parseFloat(el.value) || 0
  val = Math.round((val + delta) / step) * step
  val = Math.min(max, Math.max(min, val))
  el.value = val
  el.dispatchEvent(new Event('input', { bubbles: true }))
}

// ── Theme Toggle ──
window.toggleTheme = function () {
  const html = document.documentElement
  const isLight = html.getAttribute('data-theme') === 'light'
  if (isLight) {
    html.removeAttribute('data-theme')
    localStorage.setItem('polybot-theme', 'dark')
  } else {
    html.setAttribute('data-theme', 'light')
    localStorage.setItem('polybot-theme', 'light')
  }
  updateThemeIcons()
}

function updateThemeIcons() {
  const isLight = document.documentElement.getAttribute('data-theme') === 'light'
  document.querySelectorAll('.sun-icon').forEach((el) => el.classList.toggle('hidden', isLight))
  document.querySelectorAll('.moon-icon').forEach((el) => el.classList.toggle('hidden', !isLight))
  const label = $('themeLabel-desktop')
  if (label) label.textContent = isLight ? 'Dark' : 'Light'
}

// Restore saved theme
const saved = localStorage.getItem('polybot-theme')
if (saved === 'light') {
  document.documentElement.setAttribute('data-theme', 'light')
}
updateThemeIcons()

// ── API Credentials ──
async function loadEnv() {
  try {
    const data = await api.get('env')
    for (const [key, val] of Object.entries(data)) {
      const el = document.getElementById('env-' + key)
      if (el) {
        if (el.tagName === 'SELECT') {
          for (const opt of el.options) {
            if (opt.value === val) { opt.selected = true; break }
          }
        } else {
          el.value = val
        }
      }
    }
  } catch (e) {
    // env endpoint may not exist yet
  }
}

window.saveEnv = async function () {
  const data = {}
  document.querySelectorAll('[id^="env-"]').forEach((el) => {
    const key = el.id.replace('env-', '')
    data[key] = el.value
  })
  try {
    const res = await api.post('env', data)
    if (res.status === 'ok') {
      toast('Credentials saved to .env')
      loadEnv()
    } else {
      toast('Error: ' + (res.message || 'unknown'))
    }
  } catch (e) {
    toast('Failed to save: ' + e.message)
  }
}

// Expose for onclick / debugging
window.loadEnv = loadEnv

// Load env vars after config
setTimeout(loadEnv, 500)
