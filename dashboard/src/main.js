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

// ── Tab Switching ──
document.querySelectorAll('.tab-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach((b) => b.classList.remove('tab-active'))
    btn.classList.add('tab-active')
    document.querySelectorAll('.tab-panel').forEach((p) => p.classList.add('hidden'))
    const tab = document.getElementById('tab-' + btn.dataset.tab)
    if (tab) tab.classList.remove('hidden')
    activeTab = btn.dataset.tab

    if (activeTab === 'logs') {
      fetchLogs()
      startLogPoll()
    } else {
      stopLogPoll()
    }
    if (activeTab === 'trades') fetchTrades(window._tradeFilter || 'all')
    if (activeTab === 'bayesian') fetchBayesian()
  })
})

// ── Data Fetching ──
async function fetchAll() {
  try {
    const data = await api.getStatus()
    render(data)
  } catch (e) {
    setOffline(e.message)
  }
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

// Listen for refresh and settings-saved events
addEventListener('refresh', fetchAll)
addEventListener('settings-saved', fetchAll)
