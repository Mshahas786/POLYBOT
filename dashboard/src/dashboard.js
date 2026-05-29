import * as api from './api.js'

let prevBtc = 0
let tradeHistory = []

const $ = (id) => document.getElementById(id)

function color(value, thresholds) {
  if (!thresholds) return 'var(--color-text)'
  const { high, mid } = thresholds
  if (value >= high) return 'var(--color-green)'
  if (value >= mid) return 'var(--color-accent)'
  return 'var(--color-red)'
}

function pnlStr(pnl) {
  return (pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(2)
}

// ── Main Render ──
export function render(data) {
  const running = data.running
  const dry = data.dry_run
  const risk = data.risk || {}
  const info = data.info || {}
  const sigs = info.signals || {}
  const blocked = risk.circuit_breaker_tripped
  const streakBlocked = risk.consecutive_losses >= 3
  const isBlocked = blocked || streakBlocked

  // Status
  const sl = $('serverLabel')
  if (sl) {
    sl.textContent = isBlocked ? 'Circuit Breaker' : 'Online'
    sl.style.color = isBlocked ? 'var(--color-accent)' : 'var(--color-green)'
  }
  $('engineState').textContent = running ? 'RUNNING' : 'STOPPED'
  $('engineState').style.color = running ? 'var(--color-green)' : 'var(--color-red)'

  $('timeDisplay').textContent = new Date().toLocaleTimeString()

  // Prices
  const btc = data.btc_price || 0
  $('btcPrice').textContent = btc ? '$' + btc.toLocaleString('en-US', { minimumFractionDigits: 2 }) : '--'
  $('btcPrice').style.color = btc ? 'var(--color-green)' : 'var(--color-text2)'
  if (btc && prevBtc > 0) {
    const dlt = ((btc - prevBtc) / prevBtc * 100).toFixed(2)
    $('btcDelta').textContent = (dlt > 0 ? '+' : '') + dlt + '%'
    $('btcDelta').style.color = dlt >= 0 ? 'var(--color-green)' : 'var(--color-red)'
  } else {
    $('btcDelta').textContent = '--'
  }
  prevBtc = btc

  const cl = data.chainlink_price || 0
  $('chainlinkPrice').textContent = cl ? '$' + cl.toLocaleString('en-US', { minimumFractionDigits: 2 }) : 'OFFLINE'
  $('chainlinkPrice').style.color = cl ? 'var(--color-text)' : 'var(--color-red)'

  const lag = data.lag_score || 0
  $('clLag').textContent = lag ? 'Lag: ' + (lag > 0 ? '+' : '') + lag.toFixed(2) + '%' : '--'
  $('clLag').style.color = lag > 0.5 ? 'var(--color-red)' : lag > 0.15 ? 'var(--color-accent)' : 'var(--color-text2)'

  if (info.slug) {
    $('polyMarket').textContent = info.slug.replace('btc-updown-5m-', '')
    $('polyMarket').style.color = 'var(--color-accent)'
    const upPct = Math.round((info.up_price || 0) * 100)
    const dnPct = Math.round((info.down_price || 0) * 100)
    $('polyTokens').textContent = 'UP ' + upPct + '% / DOWN ' + dnPct + '%'
  } else {
    $('polyMarket').textContent = 'SCANNING'
    $('polyMarket').style.color = 'var(--color-text2)'
    $('polyTokens').textContent = '--'
  }

  // Alerts
  const ac = $('alertsContainer')
  let alerts = ''
  if (blocked) alerts += '<div class="alert-banner alert-danger">&#9888; Circuit Breaker: ' + (risk.circuit_breaker_reason || 'unknown') + '</div>'
  if (risk.win_rate_reduced) alerts += '<div class="alert-banner alert-warning">&#9888; Win rate reduced — bet sizes halved</div>'
  if (streakBlocked) alerts += '<div class="alert-banner alert-danger">&#9888; ' + risk.consecutive_losses + ' consecutive losses — trading blocked</div>'
  if (!running && !blocked) alerts += '<div class="alert-banner alert-warning">&#9654; Engine stopped — click Start in Settings</div>'
  if (data.chainlink_stale_seconds > 45) alerts += '<div class="alert-banner alert-warning">&#9888; Chainlink stale: ' + data.chainlink_stale_seconds.toFixed(0) + 's</div>'
  ac.innerHTML = alerts

  // Market Window
  const secs = info.time_remaining
  if (secs !== undefined) {
    const m = Math.floor(secs / 60), s = secs % 60
    $('winTime').textContent = m + ':' + s.toString().padStart(2, '0')
    $('winTime').style.color = secs <= 30 ? 'var(--color-red)' : secs <= 120 ? 'var(--color-accent)' : 'var(--color-text)'
    const circumference = 213.6
    const offset = circumference - (secs / 300) * circumference
    $('cdArc').setAttribute('stroke-dashoffset', offset)
    $('cdArc').setAttribute('stroke', secs <= 30 ? '#ff4757' : secs <= 120 ? '#f0b429' : '#00e5a0')

    $('winPhase').textContent = info.phase !== undefined ? 'Phase ' + info.phase : '--'
    $('winPhase').style.color = info.phase >= 3 ? 'var(--color-green)' : info.phase >= 2 ? 'var(--color-accent)' : 'var(--color-text2)'

    const ptb = info.price_to_beat
    $('winStrike').textContent = ptb ? '$' + ptb.toLocaleString('en-US', { minimumFractionDigits: 2 }) : '--'
    $('winUp').textContent = info.up_price !== undefined ? (info.up_price * 100).toFixed(1) + '%' : '--'
    $('winDown').textContent = info.down_price !== undefined ? (info.down_price * 100).toFixed(1) + '%' : '--'

    const diff = info.current_diff
    $('winDiff').textContent = diff !== undefined ? (diff > 0 ? '+' : '') + diff.toFixed(3) + '%' : '--'
    $('winDiff').style.color = diff > 0 ? 'var(--color-green)' : diff < 0 ? 'var(--color-red)' : 'var(--color-text2)'
  }

  // Signal
  const dir = sigs.signal_type && sigs.signal_type !== 'NONE' ? sigs.signal_type : 'NONE'
  $('sigType').textContent = dir
  const sigColors = { CORE_SNIPER: 'g', ARB: 'b', WALL_BIAS: 'y', WALL_MOMENTUM: 'g', MOMENTUM_ONLY: 'y', ORACLE_LAG: 'b', MEAN_REVERSION: 'purple', VOL_EDGE: 'b', NONE: 'r' }
  const sc = sigColors[dir] || 'b'
  const badgeMap = { g: 'bg-green/12 text-green border-green/20', b: 'bg-blue/12 text-blue border-blue/20', y: 'bg-accent/12 text-accent border-accent/20', r: 'bg-red/12 text-red border-red/20', purple: 'bg-purple/12 text-purple border-purple/20' }
  $('sigType').className = 'text-[7px] px-2 py-[3px] rounded font-mono font-semibold border ' + (badgeMap[sc] || badgeMap.b)

  const conf = info.confidence || 0
  const signalDir = sigs.bin_dir || sigs.odds_dir || (sigs.signal_type !== 'NONE' && sigs.mom_dir ? (info.edge ? info.edge.split(' ')[0] : null) : null)
  if (conf > 0) {
    $('sigDir').textContent = sigs.signal_type === 'ARB' ? 'ARB' : signalDir || '--'
    $('sigDir').style.color = signalDir === 'UP' || sigs.signal_type === 'ARB' ? 'var(--color-green)' : signalDir === 'DOWN' ? 'var(--color-red)' : 'var(--color-text2)'
    $('sigConf').textContent = conf.toFixed(0) + '%'
    $('sigConf').style.color = conf >= 75 ? 'var(--color-green)' : conf >= 70 ? 'var(--color-accent)' : 'var(--color-red)'
    const bar = $('sigBar')
    bar.style.width = Math.min(conf, 100) + '%'
    bar.style.background = conf >= 75 ? 'var(--color-green)' : conf >= 70 ? 'var(--color-accent)' : 'var(--color-red)'
  } else {
    $('sigDir').textContent = '--'
    $('sigDir').style.color = 'var(--color-text2)'
    $('sigConf').textContent = '--'
    $('sigConf').style.color = 'var(--color-text2)'
    $('sigBar').style.width = '0%'
  }

  const metaEl = $('sigMeta')
  const parts = []
  if (sigs.priority) parts.push('P' + sigs.priority)
  if (sigs.wall_ratio) parts.push('Wall: <strong>' + sigs.wall_ratio.toFixed(2) + '</strong>')
  if (sigs.lag_score) parts.push('Lag: <strong>' + (sigs.lag_score > 0 ? '+' : '') + sigs.lag_score.toFixed(2) + '%</strong>')
  if (sigs.delta_price !== undefined) parts.push('Mom: <strong>' + (sigs.delta_price > 0 ? '+' : '') + sigs.delta_price.toFixed(1) + '</strong>')
  if (sigs.mom_dir) parts.push('<span style="color:' + (sigs.mom_dir === 'UP' ? 'var(--color-green)' : 'var(--color-red)') + '">' + sigs.mom_dir + '</span>')
  if (sigs.wall_dir && sigs.wall_dir !== 'NEUTRAL') parts.push('WallDir: <strong style="color:' + (sigs.wall_dir === 'UP' ? 'var(--color-green)' : 'var(--color-red)') + '">' + sigs.wall_dir + '</strong>')
  if (sigs.bayes_mod !== undefined) parts.push('Bayes: <strong style="color:' + (sigs.bayes_mod >= 0 ? 'var(--color-green)' : 'var(--color-red)') + '">' + (sigs.bayes_mod > 0 ? '+' : '') + sigs.bayes_mod + '</strong>')
  if (info.volatility_pct !== undefined) parts.push('Vol: <strong style="color:' + (info.volatility_pct > 0.5 ? 'var(--color-red)' : info.volatility_pct > 0.3 ? 'var(--color-accent)' : 'var(--color-green)') + '">' + info.volatility_pct.toFixed(2) + '%</strong>')
  if (info.trend_pct !== undefined) parts.push('Trend: <strong style="color:' + (info.trend_pct > 0.15 ? 'var(--color-green)' : info.trend_pct < -0.15 ? 'var(--color-red)' : 'var(--color-text2)') + '">' + (info.trend_pct > 0 ? '+' : '') + info.trend_pct.toFixed(3) + '%</strong>')
  if (sigs.fair_prob !== undefined) parts.push('Fair: <strong>' + (sigs.fair_prob * 100).toFixed(1) + '%</strong>')
  if (sigs.market_prob !== undefined) parts.push('Market: <strong>' + (sigs.market_prob * 100).toFixed(1) + '%</strong>')
  if (sigs.edge !== undefined) parts.push('Edge: <strong style="color:var(--color-green)">+' + (sigs.edge * 100).toFixed(1) + '%</strong>')
  if (sigs.period_vol !== undefined) parts.push('PerVol: <strong>' + (sigs.period_vol * 100).toFixed(2) + '%</strong>')
  if (sigs.max_edge !== undefined) parts.push('MaxEdge: <strong>' + (sigs.max_edge * 100).toFixed(1) + '%</strong>')
  metaEl.innerHTML = parts.join(' &middot; ')

  // Performance
  const wins = data.wins || 0
  const losses = data.losses || 0
  const total = data.total_trades || 0
  const wr = total > 0 ? (wins / (wins + losses) * 100) : 0
  $('perfWR').textContent = wr ? wr.toFixed(1) + '%' : '--'
  $('perfWR').style.color = wr >= 55 ? 'var(--color-green)' : wr >= 45 ? 'var(--color-accent)' : 'var(--color-red)'
  $('perfTotal').textContent = total
  $('perfWL').textContent = wins + 'W / ' + losses + 'L'

  const pnl = risk.daily_pnl !== undefined ? risk.daily_pnl : (data.total_pnl || 0)
  $('perfPNL').textContent = pnlStr(pnl)
  $('perfPNL').style.color = pnl >= 0 ? 'var(--color-green)' : 'var(--color-red)'

  $('perfBankroll').textContent = risk.current_bankroll !== undefined ? '$' + risk.current_bankroll.toFixed(2) : '--'
  const dd = risk.drawdown_pct || 0
  $('perfDD').textContent = dd ? dd.toFixed(2) + '%' : '--'
  $('perfDD').style.color = dd >= 10 ? 'var(--color-red)' : dd >= 5 ? 'var(--color-accent)' : 'var(--color-text2)'

  // P&L Sparkline
  if (risk.daily_pnl !== undefined && total > 0) {
    tradeHistory.push({ pnl: risk.daily_pnl, time: Date.now() })
    if (tradeHistory.length > 60) tradeHistory.shift()
    drawSparkline($('pnlSparkline'), tradeHistory)
  }

  // Indicators
  $('indWall').textContent = data.wall_ratio !== undefined ? data.wall_ratio.toFixed(2) : '--'
  $('indWall').style.color = data.wall_ratio >= 2 ? 'var(--color-green)' : data.wall_ratio <= 0.5 ? 'var(--color-red)' : 'var(--color-text2)'
  $('indLag').textContent = data.lag_score !== undefined ? (data.lag_score > 0 ? '+' : '') + data.lag_score.toFixed(2) + '%' : '--'
  $('indLag').style.color = data.lag_score >= 0.5 ? 'var(--color-red)' : data.lag_score >= 0.15 ? 'var(--color-accent)' : 'var(--color-text2)'
  $('indMomentum').textContent = sigs.delta_price !== undefined ? (sigs.delta_price > 0 ? '+' : '') + sigs.delta_price.toFixed(1) : '--'
  $('indMomentum').style.color = sigs.delta_price > 30 ? 'var(--color-green)' : sigs.delta_price < -30 ? 'var(--color-red)' : 'var(--color-text2)'
  $('indAccel').textContent = sigs.acceleration !== undefined ? (sigs.acceleration > 0 ? '+' : '') + sigs.acceleration.toFixed(1) : '--'
  $('indVol').textContent = info.volatility_pct !== undefined ? info.volatility_pct.toFixed(2) + '%' : '--'
  $('indVol').style.color = info.volatility_pct > 0.5 ? 'var(--color-red)' : info.volatility_pct > 0.3 ? 'var(--color-accent)' : 'var(--color-text2)'
  $('indTrend').textContent = info.trend_pct !== undefined ? (info.trend_pct > 0 ? '+' : '') + info.trend_pct.toFixed(3) + '%' : '--'

  // Risk
  const rb = $('riskBadge')
  rb.textContent = isBlocked ? 'BLOCKED' : 'OK'
  rb.className = 'text-[7px] px-2 py-[3px] rounded font-mono font-semibold border ' + (isBlocked ? 'bg-red/12 text-red border-red/20' : 'bg-green/12 text-green border-green/20')

  let rh = '<div class="grid grid-cols-2 gap-1 font-mono text-[9px]">'
  const addStat = (l, v, c) => { rh += '<div class="flex justify-between items-center py-1 border-b border-border/30 text-[10px]"><span class="text-text2">' + l + '</span><span class="font-medium font-mono" style="color:' + (c || 'var(--color-text)') + '">' + v + '</span></div>' }
  addStat('Daily P&L', pnlStr(risk.daily_pnl || 0), (risk.daily_pnl || 0) >= 0 ? 'var(--color-green)' : 'var(--color-red)')
  addStat('Daily W/L', (risk.daily_wins || 0) + 'W / ' + (risk.daily_losses || 0) + 'L')
  addStat('Consecutive Losses', risk.consecutive_losses, (risk.consecutive_losses || 0) >= 3 ? 'var(--color-red)' : 'var(--color-green)')
  addStat('Hourly P&L', pnlStr(risk.hourly_pnl || 0), (risk.hourly_pnl || 0) >= 0 ? 'var(--color-green)' : 'var(--color-red)')
  addStat('Bankroll', '$' + (risk.current_bankroll || 0).toFixed(2))
  addStat('Drawdown', (dd || 0).toFixed(2) + '%', dd >= 10 ? 'var(--color-red)' : dd >= 5 ? 'var(--color-accent)' : 'var(--color-text2)')
  rh += '</div>'
  $('riskContent').innerHTML = rh

  // Module Status
  const mg = $('moduleGrid')
  const mods = data.module_status || {}
  mg.innerHTML = Object.entries(mods).map(([k, v]) => {
    const active = v !== 'INACTIVE' && v !== 'DISABLED' && v !== 'STOPPED'
    return '<div class="flex items-center gap-1.5 py-[3px] font-mono text-[9px]">' +
      '<span class="inline-block w-[5px] h-[5px] rounded-full ' + (active ? 'bg-green shadow-[0_0_4px_rgba(0,229,160,0.5)]' : 'bg-red shadow-[0_0_4px_rgba(255,71,87,0.5)]') + '"></span>' +
      '<span class="text-text2">' + k.replace(/_/g, ' ') + '</span>' +
      '<span class="ml-auto font-semibold text-[8px] ' + (active ? 'text-green' : 'text-red') + '">' + v + '</span>' +
    '</div>'
  }).join('')
}

function drawSparkline(canvas, history) {
  if (!canvas || history.length < 2) return
  const dpr = window.devicePixelRatio || 1
  const rect = canvas.getBoundingClientRect()
  canvas.width = rect.width * dpr
  canvas.height = rect.height * dpr
  const ctx = canvas.getContext('2d')
  ctx.scale(dpr, dpr)
  const w = rect.width, h = rect.height

  const values = history.map(p => p.pnl)
  const min = Math.min(...values)
  const max = Math.max(...values)
  const range = max - min || 1
  const pad = 4

  ctx.clearRect(0, 0, w, h)

  // Grid line
  ctx.strokeStyle = '#2a2a3e'
  ctx.lineWidth = 0.5
  ctx.beginPath()
  ctx.moveTo(0, h / 2)
  ctx.lineTo(w, h / 2)
  ctx.stroke()

  // Line
  ctx.strokeStyle = values[values.length - 1] >= values[0] ? '#00e5a0' : '#ff4757'
  ctx.lineWidth = 1.5
  ctx.beginPath()
  values.forEach((v, i) => {
    const x = (i / (values.length - 1)) * w
    const y = h - pad - ((v - min) / range) * (h - pad * 2)
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)
  })
  ctx.stroke()

  // Fill
  const lastX = w
  const lastY = h - pad - ((values[values.length - 1] - min) / range) * (h - pad * 2)
  ctx.lineTo(lastX, h)
  ctx.lineTo(0, h)
  ctx.closePath()
  const grad = ctx.createLinearGradient(0, 0, 0, h)
  const c = values[values.length - 1] >= values[0] ? 'rgba(0,229,160,' : 'rgba(255,71,87,'
  grad.addColorStop(0, c + '0.2)')
  grad.addColorStop(1, c + '0.02)')
  ctx.fillStyle = grad
  ctx.fill()
}

// ── Trades ──
export async function fetchTrades(filter) {
  try {
    const d = await api.getStats(filter)
    const trades = (d.history || []).slice().reverse()
    const tb = $('tradeBody')
    if (trades.length) {
      $('tradeEmpty').classList.add('hidden')
      $('tradeSummary').textContent = d.total_trades + ' trades | ' + d.wins + 'W ' + d.losses + 'L | ' + pnlStr(d.pnl) + ' | ' + d.success_rate + '% WR'
      tb.innerHTML = trades.map(t => {
        const dir = t.direction || '--'
        const isWin = t.outcome === 'win', isLoss = t.outcome === 'loss'
        const pnl = t.pnl
        const st = t.signals || {}
        return '<tr class="border-b border-border/30 hover:bg-surface2/50">' +
          '<td class="px-2 py-1.5 text-muted">' + (t.timestamp ? new Date(t.timestamp).toLocaleTimeString() : '--') + '</td>' +
          '<td class="px-2 py-1.5 font-semibold" style="color:' + (dir === 'UP' ? 'var(--color-green)' : dir === 'DOWN' ? 'var(--color-red)' : 'var(--color-text2)') + '">' + dir + '</td>' +
          '<td class="px-2 py-1.5">$' + (t.bet_size || 0).toFixed(2) + '</td>' +
          '<td class="px-2 py-1.5 text-text2">' + (t.token_price ? (t.token_price * 100).toFixed(1) + '%' : '--') + '</td>' +
          '<td class="px-2 py-1.5 text-text2 text-[8px]">' + (st.signal_type || '--') + '</td>' +
          '<td class="px-2 py-1.5">' + (t.confidence ? t.confidence.toFixed(0) + '%' : '--') + '</td>' +
          '<td class="px-2 py-1.5 font-semibold" style="color:' + (isWin ? 'var(--color-green)' : isLoss ? 'var(--color-red)' : 'var(--color-text2)') + '">' + (isWin ? 'WIN' : isLoss ? 'LOSS' : t.status || '--') + '</td>' +
          '<td class="px-2 py-1.5 font-semibold" style="color:' + (pnl >= 0 ? 'var(--color-green)' : pnl < 0 ? 'var(--color-red)' : 'var(--color-text2)') + '">' + (pnl !== undefined && pnl !== null ? pnlStr(pnl) : '--') + '</td>' +
        '</tr>'
      }).join('')
    } else {
      tb.innerHTML = ''
      $('tradeEmpty').classList.remove('hidden')
      $('tradeSummary').textContent = ''
    }
  } catch (e) { console.error(e) }
}

// ── Bayesian ──
export async function fetchBayesian() {
  try {
    const d = await api.getBayesian()
    const buckets = d.buckets || []
    const bc = $('bayesianContent')
    if (buckets.length) {
      bc.innerHTML = '<div class="flex gap-1.5 px-2 py-1.5 text-[8px] text-muted font-mono border-b border-border"><span class="flex-1">Bucket</span><span class="w-[40px]">Rate</span><span class="w-[25px]">N</span><span class="w-[40px]">Bar</span></div>' +
        buckets.map(b => {
          const wr = b.win_rate || 0
          return '<div class="flex justify-between items-center px-1.5 py-1 font-mono text-[9px] border-b border-border/15">' +
            '<span class="text-text2 flex-1">' + b.bucket + '</span>' +
            '<span class="font-semibold min-w-[35px] text-right" style="color:' + (wr >= 55 ? 'var(--color-green)' : wr >= 45 ? 'var(--color-accent)' : 'var(--color-red)') + '">' + wr.toFixed(0) + '%</span>' +
            '<span class="text-muted min-w-[25px] text-right">' + b.total + '</span>' +
            '<div class="w-[40px] h-1 bg-bg rounded overflow-hidden flex-shrink-0 ml-1"><div class="h-full rounded" style="width:' + Math.min(wr, 100) + '%;background:' + (wr >= 55 ? 'var(--color-green)' : wr >= 45 ? 'var(--color-accent)' : 'var(--color-red)') + '"></div></div>' +
          '</div>'
        }).join('')
    } else {
      bc.innerHTML = '<div class="py-8 text-center text-muted font-mono text-[10px]">No Bayesian data yet — run trades first</div>'
    }
  } catch (e) { /* silent */ }
}

document.addEventListener('DOMContentLoaded', function () {
  const daysInput = $('daysInput')
  if (daysInput) {
    daysInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') window.setFilter('days')
    })
  }
})

// ── Logs ──
export async function fetchLogs() {
  try {
    const d = await api.getLogs()
    const logs = d.logs || []
    $('logBox').innerHTML = [...logs].reverse().map(l => {
      const p = l.split(' ')
      const ts = p[1] ? p[1].split(',')[0] : ''
      const rest = p.slice(2).join(' ')
      let c = 'info'
      if (/WARN|BLOCK/.test(rest)) c = 'warn'
      if (/ERROR|Fail|Error/.test(rest)) c = 'err'
      if (/WIN|SUCCESS/.test(rest)) c = 'ok'
      const colors = { info: 'text-text2', warn: 'text-accent', err: 'text-red', ok: 'text-green' }
      return '<div class="flex gap-1.5 py-[3px] px-1.5 font-mono text-xs lg:text-sm border-b border-border/15"><span class="text-muted flex-shrink-0">' + ts + '</span><span class="' + (colors[c] || 'text-text2') + '">' + (rest || l) + '</span></div>'
    }).join('')
    $('logCount').textContent = logs.length + ' lines'
  } catch (e) { /* silent */ }
}

// ── Config Load ──
export async function loadConfig() {
  try {
    const c = await api.getConfig()
    if (!c) {
      toast('Failed to load config — API returned empty', 'err')
      return
    }
    const dry = c.dry_run !== false
    $('toggleDry').className = 'toggle-switch' + (dry ? ' toggle-on' : '')
    $('liveWarn').classList.toggle('hidden', dry)
    $('sBetSize').value = c.bet_size || 1
    $('sMaxBet').value = c.max_bet || 3
    $('sMaxTrades').value = c.max_trades_per_hour || 12
    $('sMinConf').value = c.min_confidence || 80
    const rm = c.risk_management || {}
    $('sDailyLoss').value = rm.daily_loss_limit_usdc || 6
    $('sHourlyLoss').value = rm.hourly_loss_pct || 5
    $('sMaxLossStreak').value = rm.max_consecutive_losses || 4
    $('sMaxDD').value = rm.max_drawdown_pct || 10
    $('sMaxConcurrent').value = rm.max_concurrent_positions || 3
    $('sCooldown').value = rm.cooldown_after_loss_seconds || 300
    $('sHourlyCooldown').value = rm.cooldown_after_hourly_loss_seconds || 3600
    $('sStaleFeed').value = rm.stale_feed_seconds || 60
    $('sBaseBetPct').value = rm.base_bet_pct || 1
    $('sHighConfBetPct').value = rm.high_conf_bet_pct || 2
    $('sArbBetPct').value = rm.arb_bet_pct || 5
    const kelly = rm.use_kelly_sizing || false
    $('toggleKelly').className = 'toggle-switch' + (kelly ? ' toggle-on' : '')
    $('sKellyFrac').value = rm.kelly_fraction || 0.3
    $('sInitBankroll').value = rm.initial_bankroll || 100
    $('sMaxTokenPrice').value = rm.max_token_price || 0.92
    $('sToxicLag').value = rm.toxic_lag_threshold || 1.0
    $('sEdgeLagMax').value = rm.edge_lag_max || 1.5
    $('sEdgeLagMin').value = rm.edge_lag_min || 0.3
    $('sWinRateLb').value = rm.auto_stop_win_rate_lookback_trades || 20
    $('sWinRateThresh').value = rm.auto_stop_win_rate_threshold || 45
    $('sClFallbackInt').value = c.strategy?.chainlink_fallback_interval || 10
    $('sOutcomeInt').value = c.strategy?.outcome_check_interval || 120
    $('sMarketInt').value = c.strategy?.market_fetch_interval || 30
    $('sBalanceInt').value = c.strategy?.balance_fetch_interval || 30
    $('sSigCheckInt').value = c.strategy?.signal_check_interval || 10
    // Strategy tuning
    const sk = c.strategy || {}
    $('sWallUp').value = sk.wall_ratio_up_threshold || 2.5
    $('sWallDown').value = sk.wall_ratio_down_threshold || 0.4
    $('sWallMax').value = sk.wall_ratio_max || 5.0
    $('sMomStrong').value = sk.momentum_delta_strengthening || 50
    $('sMomMod').value = sk.momentum_delta_moderate || 25
    $('sMomBlock').value = sk.momentum_block_delta || 20
    $('sMomOppCap').value = sk.momentum_opposition_cap || 65
    $('sLatMove').value = sk.latency_arb_move_pct || 0.3
    $('sLatToken').value = sk.latency_arb_max_token_price || 0.70
    $('sWdDelta').value = sk.window_delta_min_delta_pct || 0.2
    $('sWdToken').value = sk.window_delta_max_token_price || 0.70
    $('sWdTimeMax').value = sk.window_delta_time_max || 50
    $('sWdTimeMin').value = sk.window_delta_time_min || 10
    $('sCsThresh').value = sk.cheap_side_threshold || 0.85
    $('sVolThresh').value = sk.vol_edge_threshold || 0.05
    $('sArbThresh').value = sk.arb_threshold || 0.985
    $('sHedgeThresh').value = sk.hedge_threshold || 0.99
    $('sBayesMin').value = sk.bayesian_min_trades || 3
    $('sBayesBoostHigh').value = sk.bayesian_boost_high_amt || 5
    $('sBayesBoostLow').value = sk.bayesian_boost_low_amt || 3
    $('sBayesPenHigh').value = sk.bayesian_penalty_high_amt || -10
    $('sBayesPenLow').value = sk.bayesian_penalty_low_amt || -5
    $('sResT3').value = sk.resolution_hunt_t3_threshold || 0.04
    $('sResT5').value = sk.resolution_hunt_t5_threshold || 0.04
    $('sResT10').value = sk.resolution_hunt_t10_threshold || 0.03
    $('sResT20').value = sk.resolution_hunt_t20_threshold || 0.05
    $('sResBase').value = sk.resolution_hunt_base_threshold || 0.08
    $('sPhase1').value = sk.phase1_min_seconds || 250
    $('sPhase2').value = sk.phase2_min_seconds || 100
    $('sPhase3').value = sk.phase3_min_seconds || 30
    $('sPhase4').value = sk.phase4_min_seconds || 5
    $('sSigGuardCD').value = sk.signal_guard_cooldown || 120
    $('sTrendThresh').value = sk.trend_bias_threshold || 0.15
    $('sFeeBuf').value = sk.fee_buffer_pp || 5
    $('sVolBlock').value = sk.volatility_block_spread_pct || 0.5
    $('sVolRatio').value = sk.volume_ratio_threshold || 0.5
    $('sEdgeMidLow').value = sk.edge_token_mid_low || 0.47
    $('sEdgeMidHigh').value = sk.edge_token_mid_high || 0.53
    $('sPriceMarkup').value = sk.order_price_markup || 1.05
    $('sMaxOrdPrice').value = sk.max_order_price || 0.99
    $('sTrendPen').value = sk.trend_mismatch_penalty || 15
    $('sTrendBonus').value = sk.trend_match_bonus || 5
    // Modules
    const mods = c.modules || {}
    const modIds = { modSignalArb: 'signal_arb', modDeltaOverride: 'signal_delta_override', modLatencyArb: 'signal_latency_arb', modWindowDelta: 'signal_window_delta', modCheapSide: 'signal_cheap_side', modVolEdge: 'signal_vol_edge', modWallMomentum: 'signal_wall_momentum', modWallBias: 'signal_wall_bias', modOracleLag: 'signal_oracle_lag', modMomentumOnly: 'signal_momentum_only', modMeanReversion: 'signal_mean_reversion', modResolutionHunt: 'resolution_hunting', modHardDeadline: 'hard_deadline', modArbHedge: 'arb_hedge' }
    for (const [id, key] of Object.entries(modIds)) {
      const el = $(id)
      if (el) el.className = 'toggle-switch' + (mods[key] !== false ? ' toggle-on' : '')
    }
    // Guards
    const guards = c.guards || {}
    const guardIds = { guardMomentum: 'momentum_consistency', guardVolatility: 'volatility_guard', guardVolume: 'volume_confirmation', guardTrend: 'trend_bias_filter', guardConsecutive: 'consecutive_loss_guard', guardStaleFeed: 'stale_feed_guard', guardSignal: 'signal_guard', guardFee: 'fee_aware_gate', guardEdge: 'edge_block' }
    for (const [id, key] of Object.entries(guardIds)) {
      const el = $(id)
      if (el) el.className = 'toggle-switch' + (guards[key] !== false ? ' toggle-on' : '')
    }
  } catch (e) {
    toast('Failed to load config: ' + e.message, 'err')
  }
}

// ── Settings Save ──
export async function saveSettings() {
  const dry = $('toggleDry').classList.contains('toggle-on')
  const modIds = { modSignalArb: 'signal_arb', modDeltaOverride: 'signal_delta_override', modLatencyArb: 'signal_latency_arb', modWindowDelta: 'signal_window_delta', modCheapSide: 'signal_cheap_side', modVolEdge: 'signal_vol_edge', modWallMomentum: 'signal_wall_momentum', modWallBias: 'signal_wall_bias', modOracleLag: 'signal_oracle_lag', modMomentumOnly: 'signal_momentum_only', modMeanReversion: 'signal_mean_reversion', modResolutionHunt: 'resolution_hunting', modHardDeadline: 'hard_deadline', modArbHedge: 'arb_hedge' }
  const guardIds = { guardMomentum: 'momentum_consistency', guardVolatility: 'volatility_guard', guardVolume: 'volume_confirmation', guardTrend: 'trend_bias_filter', guardConsecutive: 'consecutive_loss_guard', guardStaleFeed: 'stale_feed_guard', guardSignal: 'signal_guard', guardFee: 'fee_aware_gate', guardEdge: 'edge_block' }
  const cfg = {
    dry_run: dry,
    bet_size: parseFloat($('sBetSize').value) || 1,
    max_bet: parseFloat($('sMaxBet').value) || 3,
    max_trades_per_hour: parseInt($('sMaxTrades').value) || 12,
    min_confidence: parseInt($('sMinConf').value) || 80,
    _confirm_live: !dry,
    risk_management: {
      enabled: true,
      daily_loss_limit_usdc: parseFloat($('sDailyLoss').value) || 6,
      hourly_loss_pct: parseFloat($('sHourlyLoss').value) || 5,
      max_consecutive_losses: parseInt($('sMaxLossStreak').value) || 4,
      max_drawdown_pct: parseFloat($('sMaxDD').value) || 10,
      max_concurrent_positions: parseInt($('sMaxConcurrent').value) || 3,
      cooldown_after_loss_seconds: parseInt($('sCooldown').value) || 300,
      cooldown_after_hourly_loss_seconds: parseInt($('sHourlyCooldown').value) || 3600,
      stale_feed_seconds: parseInt($('sStaleFeed').value) || 60,
      base_bet_pct: parseFloat($('sBaseBetPct').value) || 1,
      high_conf_bet_pct: parseFloat($('sHighConfBetPct').value) || 2,
      arb_bet_pct: parseFloat($('sArbBetPct').value) || 5,
      use_kelly_sizing: $('toggleKelly').classList.contains('toggle-on'),
      kelly_fraction: parseFloat($('sKellyFrac').value) || 0.3,
      initial_bankroll: parseFloat($('sInitBankroll').value) || 100,
      max_token_price: parseFloat($('sMaxTokenPrice').value) || 0.92,
      toxic_lag_threshold: parseFloat($('sToxicLag').value) || 1.0,
      edge_lag_max: parseFloat($('sEdgeLagMax').value) || 1.5,
      edge_lag_min: parseFloat($('sEdgeLagMin').value) || 0.3,
      auto_stop_win_rate_lookback_trades: parseInt($('sWinRateLb').value) || 20,
      auto_stop_win_rate_threshold: parseFloat($('sWinRateThresh').value) || 45,
    },
    strategy: {
      chainlink_fallback_interval: parseInt($('sClFallbackInt').value) || 10,
      outcome_check_interval: parseInt($('sOutcomeInt').value) || 120,
      market_fetch_interval: parseInt($('sMarketInt').value) || 30,
      balance_fetch_interval: parseInt($('sBalanceInt').value) || 30,
      signal_check_interval: parseInt($('sSigCheckInt').value) || 10,
      wall_ratio_up_threshold: parseFloat($('sWallUp').value) || 2.5,
      wall_ratio_down_threshold: parseFloat($('sWallDown').value) || 0.4,
      wall_ratio_max: parseFloat($('sWallMax').value) || 5.0,
      momentum_delta_strengthening: parseFloat($('sMomStrong').value) || 50,
      momentum_delta_moderate: parseFloat($('sMomMod').value) || 25,
      momentum_block_delta: parseFloat($('sMomBlock').value) || 20,
      momentum_opposition_cap: parseFloat($('sMomOppCap').value) || 65,
      latency_arb_move_pct: parseFloat($('sLatMove').value) || 0.3,
      latency_arb_max_token_price: parseFloat($('sLatToken').value) || 0.70,
      window_delta_min_delta_pct: parseFloat($('sWdDelta').value) || 0.2,
      window_delta_max_token_price: parseFloat($('sWdToken').value) || 0.70,
      window_delta_time_max: parseInt($('sWdTimeMax').value) || 50,
      window_delta_time_min: parseInt($('sWdTimeMin').value) || 10,
      cheap_side_threshold: parseFloat($('sCsThresh').value) || 0.85,
      vol_edge_threshold: parseFloat($('sVolThresh').value) || 0.05,
      arb_threshold: parseFloat($('sArbThresh').value) || 0.985,
      hedge_threshold: parseFloat($('sHedgeThresh').value) || 0.99,
      bayesian_min_trades: parseInt($('sBayesMin').value) || 3,
      bayesian_boost_high_amt: parseInt($('sBayesBoostHigh').value) || 5,
      bayesian_boost_low_amt: parseInt($('sBayesBoostLow').value) || 3,
      bayesian_penalty_high_amt: parseInt($('sBayesPenHigh').value) || -10,
      bayesian_penalty_low_amt: parseInt($('sBayesPenLow').value) || -5,
      resolution_hunt_t3_threshold: parseFloat($('sResT3').value) || 0.04,
      resolution_hunt_t5_threshold: parseFloat($('sResT5').value) || 0.04,
      resolution_hunt_t10_threshold: parseFloat($('sResT10').value) || 0.03,
      resolution_hunt_t20_threshold: parseFloat($('sResT20').value) || 0.05,
      resolution_hunt_base_threshold: parseFloat($('sResBase').value) || 0.08,
      phase1_min_seconds: parseInt($('sPhase1').value) || 250,
      phase2_min_seconds: parseInt($('sPhase2').value) || 100,
      phase3_min_seconds: parseInt($('sPhase3').value) || 30,
      phase4_min_seconds: parseInt($('sPhase4').value) || 5,
      signal_guard_cooldown: parseInt($('sSigGuardCD').value) || 120,
      trend_bias_threshold: parseFloat($('sTrendThresh').value) || 0.15,
      fee_buffer_pp: parseInt($('sFeeBuf').value) || 5,
      volatility_block_spread_pct: parseFloat($('sVolBlock').value) || 0.5,
      volume_ratio_threshold: parseFloat($('sVolRatio').value) || 0.5,
      edge_token_mid_low: parseFloat($('sEdgeMidLow').value) || 0.47,
      edge_token_mid_high: parseFloat($('sEdgeMidHigh').value) || 0.53,
      order_price_markup: parseFloat($('sPriceMarkup').value) || 1.05,
      max_order_price: parseFloat($('sMaxOrdPrice').value) || 0.99,
      trend_mismatch_penalty: parseInt($('sTrendPen').value) || 15,
      trend_match_bonus: parseInt($('sTrendBonus').value) || 5,
    },
    modules: Object.fromEntries(Object.entries(modIds).map(([id, key]) => [key, $(id)?.classList.contains('toggle-on') ?? true])),
    guards: Object.fromEntries(Object.entries(guardIds).map(([id, key]) => [key, $(id)?.classList.contains('toggle-on') ?? true])),
  }
  try {
    const d = await api.saveConfig(cfg)
    if (d.status === 'saved') {
      toast('Settings saved', 'ok')
      dispatchEvent(new CustomEvent('settings-saved'))
    } else {
      toast(d.message || 'Error', 'err')
    }
  } catch (e) {
    toast(e.message, 'err')
  }
}

// ── Toast ──
export function toast(msg, type) {
  const el = $('toast')
  el.textContent = msg
  el.className = 'fixed top-[50px] left-1/2 -translate-x-1/2 -translate-y-5 bg-surface border rounded-lg px-4 py-2 font-mono text-[10px] z-[999] opacity-0 transition-all duration-300 pointer-events-none shadow-[0_8px_30px_rgba(0,0,0,0.5)]'
  const borderColors = { ok: 'border-green/30 text-green', err: 'border-red/30 text-red', '': 'border-border text-text2' }
  el.className += ' ' + (borderColors[type] || borderColors[''])
  el.style.borderColor = type === 'ok' ? 'rgba(0,229,160,0.3)' : type === 'err' ? 'rgba(255,71,87,0.3)' : 'var(--color-border)'
  el.style.color = type === 'ok' ? 'var(--color-green)' : type === 'err' ? 'var(--color-red)' : 'var(--color-text2)'
  // Trigger animation
  requestAnimationFrame(() => {
    el.style.opacity = '1'
    el.style.transform = 'translateX(-50%) translateY(0)'
  })
  setTimeout(() => {
    el.style.opacity = '0'
    el.style.transform = 'translateX(-50%) translateY(-20px)'
  }, 2500)
}

export function setOffline(err) {
  const sl = $('serverLabel')
  if (sl) { sl.textContent = 'Offline'; sl.style.color = 'var(--color-red)' }
  $('engineState').textContent = 'OFFLINE'
  $('engineState').style.color = 'var(--color-red)'
  $('btcPrice').textContent = '--'
  $('chainlinkPrice').textContent = 'OFFLINE'
  $('chainlinkPrice').style.color = 'var(--color-red)'
  $('alertsContainer').innerHTML = '<div class="alert-banner alert-danger">&#9888; Dashboard offline: API unreachable</div>'
}

// ── Export helpers exposed to global for onclick handlers ──
window.exportTrades = async function (fmt) {
  try {
    const blob = await api.exportTradesBlob(fmt)
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = 'polybot_trades.' + fmt
    document.body.appendChild(a)
    a.click()
    a.remove()
  } catch (e) { toast(e.message, 'err') }
}

window.clearLogs = async function () {
  try {
    await api.clearLogsApi()
    toast('Logs cleared', 'ok')
    fetchLogs()
  } catch (e) { toast(e.message, 'err') }
}

window.clearTrades = async function () {
  try {
    await api.clearTradesApi()
    toast('Trades cleared', 'ok')
    tradeHistory = []
    fetchTrades()
  } catch (e) { toast(e.message, 'err') }
}

window.toggleDry = function () {
  const t = $('toggleDry')
  const on = t.classList.contains('toggle-on')
  if (on && !confirm('ENABLE LIVE TRADING?')) return
  t.classList.toggle('toggle-on')
  $('liveWarn').classList.toggle('hidden', t.classList.contains('toggle-on'))
}

window.toggleKelly = function () {
  $('toggleKelly').classList.toggle('toggle-on')
}

window.toggleMod = function (id) {
  const el = $(id)
  if (el) el.classList.toggle('toggle-on')
}

window.toggleGuard = function (id) {
  const el = $(id)
  if (el) el.classList.toggle('toggle-on')
}

window.saveSettings = saveSettings

window.ctrl = async function (action) {
  try {
    await api.postAction(action)
    toast(action === 'start' ? 'Engine started' : action === 'stop' ? 'Engine stopped' : 'Restarted', 'ok')
    setTimeout(() => dispatchEvent(new CustomEvent('refresh')), 1500)
  } catch (e) { toast(e.message, 'err') }
}

window.resetBreaker = async function () {
  if (!confirm('Reset ALL risk blocks (consecutive losses, win rate, circuit breaker)?')) return
  try {
    await api.resetRisk()
    toast('All risk blocks cleared', 'ok')
    dispatchEvent(new CustomEvent('refresh'))
  } catch (e) { toast(e.message, 'err') }
}

window.resetHard = async function () {
  if (!confirm('FACTORY RESET: Clear all trades, risk state, and Bayesian data?')) return
  if (!confirm('This PERMANENTLY deletes all history. Are you sure?')) return
  try {
    const d = await api.hardReset()
    toast(d.message || 'Reset complete', 'ok')
    setTimeout(() => dispatchEvent(new CustomEvent('refresh')), 1500)
  } catch (e) { toast(e.message, 'err') }
}

window.setFilter = function (f) {
  if (f === 'days') {
    const val = parseInt($('daysInput').value) || 7
    f = val + 'd'
  }
  window._tradeFilter = f
  document.querySelectorAll('#tab-trades .btn-filter').forEach(b => b.classList.remove('btn-filter-active'))
  if (f === '1h') {
    const btns = document.querySelectorAll('#tab-trades .btn-filter')
    if (btns[0]) btns[0].classList.add('btn-filter-active')
  } else if (f === '24h') {
    const btns = document.querySelectorAll('#tab-trades .btn-filter')
    if (btns[1]) btns[1].classList.add('btn-filter-active')
  } else if (f === '7d') {
    const btns = document.querySelectorAll('#tab-trades .btn-filter')
    if (btns[2]) btns[2].classList.add('btn-filter-active')
  } else if (f === '30d') {
    const btns = document.querySelectorAll('#tab-trades .btn-filter')
    if (btns[3]) btns[3].classList.add('btn-filter-active')
  } else if (f.endsWith('d') && !['7d','30d'].includes(f)) {
    document.querySelector('#filterAll')?.classList.add('btn-filter-active')
  } else {
    document.querySelector('#filterAll')?.classList.add('btn-filter-active')
  }
  fetchTrades(f)
}

window.fetchBayesian = fetchBayesian

window.adjDays = function (delta) {
  const el = $('daysInput')
  if (!el) return
  const step = 1
  const min = parseInt(el.min) || 1
  const max = parseInt(el.max) || 365
  let val = (parseFloat(el.value) || 1) + delta * step
  val = Math.max(min, Math.min(max, val))
  el.value = val
  window.setFilter('days')
}
