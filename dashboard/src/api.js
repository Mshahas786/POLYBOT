export const API = 'http://127.0.0.1:3000'

export async function get(endpoint) {
  const r = await fetch(API + '/' + endpoint)
  if (!r.ok) throw new Error('HTTP ' + r.status)
  return r.json()
}

export async function post(endpoint, data) {
  const r = await fetch(API + '/' + endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  return r.json()
}

export async function getStatus() {
  const r = await fetch(API + '/status')
  if (!r.ok) throw new Error('HTTP ' + r.status)
  return r.json()
}

export async function postAction(action) {
  const r = await fetch(API + '/' + action, { method: 'POST' })
  return r.json()
}

export async function getStats(period = 'all') {
  const r = await fetch(API + '/stats?period=' + period)
  return r.ok ? r.json() : {}
}

export async function getLogs() {
  const r = await fetch(API + '/logs')
  return r.ok ? r.json() : { logs: [] }
}

export async function getBayesian() {
  const r = await fetch(API + '/bayesian')
  if (!r.ok) throw new Error('HTTP ' + r.status)
  return r.json()
}

export async function getConfig() {
  const r = await fetch(API + '/config')
  return r.json()
}

export async function saveConfig(cfg) {
  const r = await fetch(API + '/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cfg),
  })
  return r.json()
}

export async function exportTradesBlob(fmt) {
  const r = await fetch(API + '/export-trades?format=' + fmt)
  return r.blob()
}

export async function clearLogsApi() {
  return postAction('clear-logs')
}

export async function resetRisk() {
  return postAction('reset-risk')
}

export async function hardReset() {
  return postAction('hard-reset')
}
