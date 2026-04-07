import sys

def modify_dashboard():
    with open("index.html", "r", encoding="utf-8") as f:
        content = f.read()

    # 1. Add filter buttons next to REFRESH TRADES
    old_trades_header = '<button class="refresh-btn" onclick="fetchTrades()">⟳ REFRESH TRADES</button>'
    new_trades_header = '''<div style="display:flex; justify-content:space-between; margin-bottom:12px; overflow-x:auto;">
            <button class="refresh-btn" style="margin-bottom:0; flex:0 0 auto; padding:8px 12px; font-size:10px; margin-right:8px;" onclick="fetchTrades()">⟳ REFRESH</button>
            <div style="display:flex; flex-wrap:nowrap;">
                <button class="filter-btn" onclick="setFilter('30m', this)" style="padding:6px 12px; border-radius:6px; font-family:JetBrains Mono, monospace; font-size:10px; background:var(--bg); border:1px solid var(--border); color:var(--text); margin-right:4px; cursor:pointer;">30M</button>
                <button class="filter-btn" onclick="setFilter('1h', this)" style="padding:6px 12px; border-radius:6px; font-family:JetBrains Mono, monospace; font-size:10px; background:var(--bg); border:1px solid var(--border); color:var(--text); margin-right:4px; cursor:pointer;">1H</button>
                <button class="filter-btn" onclick="setFilter('6h', this)" style="padding:6px 12px; border-radius:6px; font-family:JetBrains Mono, monospace; font-size:10px; background:var(--bg); border:1px solid var(--border); color:var(--text); margin-right:4px; cursor:pointer;">6H</button>
                <button class="filter-btn" onclick="setFilter('24h', this)" style="padding:6px 12px; border-radius:6px; font-family:JetBrains Mono, monospace; font-size:10px; background:var(--bg); border:1px solid var(--border); color:var(--text); margin-right:4px; cursor:pointer;">24H</button>
                <button class="filter-btn active" onclick="setFilter('all', this)" style="padding:6px 12px; border-radius:6px; font-family:JetBrains Mono, monospace; font-size:10px; background:var(--accent-dim); border:1px solid var(--accent); color:var(--accent); cursor:pointer;">ALL</button>
            </div>
        </div>'''
    
    if old_trades_header in content:
        content = content.replace(old_trades_header, new_trades_header)
    
    # 2. Update Stats Grid
    old_grid = '''        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-value green" id="statStatus">--</div>
                <div class="stat-label">Bot Status</div>
            </div>
            <div class="stat-card">
                <div class="stat-value accent" id="statTrades">--</div>
                <div class="stat-label">Total Trades</div>
            </div>
            <div class="stat-card">
                <div class="stat-value green" id="statUp">--</div>
                <div class="stat-label">UP Trades</div>
            </div>
            <div class="stat-card">
                <div class="stat-value red" id="statDown">--</div>
                <div class="stat-label">DOWN Trades</div>
            </div>
        </div>'''
        
    new_grid = '''        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-value green" id="statStatus">--</div>
                <div class="stat-label">Status</div>
            </div>
            <div class="stat-card">
                <div class="stat-value green" id="statSuccess">--</div>
                <div class="stat-label">Win Rate %</div>
            </div>
            <div class="stat-card">
                <div class="stat-value green" id="statUp">--</div>
                <div class="stat-label">WINS</div>
            </div>
            <div class="stat-card">
                <div class="stat-value red" id="statDown">--</div>
                <div class="stat-label">LOSSES</div>
            </div>
        </div>'''
        
    if old_grid in content:
        content = content.replace(old_grid, new_grid)
        
    # 3. Add JS for setFilter and modify fetchTrades
    old_fetch_trades = '''        // ── Fetch Trades ─────────────────────────────
        async function fetchTrades() {
            try {
                const resp = await fetch(VPS_URL + '/trades?limit=50');'''
                
    new_fetch_trades = '''        // ── Fetch Trades ─────────────────────────────
        let currentFilter = 'all';
        function setFilter(p, btn) {
            currentFilter = p;
            document.querySelectorAll('.filter-btn').forEach(b => {
                b.style.background = 'var(--bg)'; b.style.color = 'var(--text)'; b.style.borderColor = 'var(--border)'; b.classList.remove('active');
            });
            btn.style.background = 'var(--accent-dim)'; btn.style.color = 'var(--accent)'; btn.style.borderColor = 'var(--accent)'; btn.classList.add('active');
            fetchTrades();
        }
        
        async function fetchTrades() {
            try {
                let data = {};
                const resp = await fetch(VPS_URL + '/stats?period=' + currentFilter);
                if (resp.ok) {
                    data = await resp.json();
                    document.getElementById('statSuccess').textContent = (data.success_rate || 0) + '%';
                    document.getElementById('statUp').textContent = data.wins || 0;
                    document.getElementById('statDown').textContent = data.losses || 0;
                    // document.getElementById('statTrades').textContent = data.total_trades || 0;
                } else {
                    const fallback = await fetch(VPS_URL + '/trades?limit=50');
                    data = await fallback.json();
                }'''
                
    if old_fetch_trades in content:
        content = content.replace(old_fetch_trades, new_fetch_trades)
        
    # 4. Modify tradeCard generation
    old_card = '''            const status = trade.dry_run ? 'simulated' : (trade.status || 'placed');
            const statusLabel = trade.dry_run ? 'SIM' : (trade.status || 'LIVE').toUpperCase();
            const size = trade.bet_size ? '$' + trade.bet_size : '';

            return `<div class="trade-card">
                <div class="trade-left">
                    <div class="trade-dir ${dirClass}">${dir === 'UP' ? '▲' : '▼'} ${dir} ${size}</div>
                    <div class="trade-meta">${date} ${time} • Conf: ${confidence}</div>
                </div>
                <div class="trade-right">
                    <div class="trade-price">${price}</div>
                    <span class="trade-status ${status}">${statusLabel}</span>
                </div>
            </div>`;'''
            
    new_card = '''            const status = trade.outcome || (trade.dry_run ? 'simulated' : (trade.status || 'placed'));
            const statusLabel = trade.outcome ? trade.outcome.toUpperCase() : (trade.dry_run ? 'SIM' : 'LIVE');
            const size = trade.bet_size ? '$' + trade.bet_size : '';
            
            let colorCls = trade.dry_run ? "simulated" : "placed";
            let borderStyle = "";
            if(trade.outcome === 'win') { colorCls = 'placed'; borderStyle = "border-left: 3px solid var(--green)"; }
            if(trade.outcome === 'loss') { colorCls = 'error'; borderStyle = "border-left: 3px solid var(--red)"; }

            return `<div class="trade-card" style="${borderStyle}">
                <div class="trade-left">
                    <div class="trade-dir ${dirClass}">${dir === 'UP' ? '▲' : '▼'} ${dir} ${size}</div>
                    <div class="trade-meta">${date} ${time} • Conf: ${confidence}</div>
                </div>
                <div class="trade-right">
                    <div class="trade-price">${price}</div>
                    <span class="trade-status ${colorCls}">${statusLabel}</span>
                </div>
            </div>`;'''
            
    if old_card in content:
        content = content.replace(old_card, new_card)
        
    # 5. Modify P&L Chart Logic
    old_pnl = '''            chart.innerHTML = last30.map(t => {
                const dir = t.direction === 'UP' ? 'up' : 'down';
                if (dir === 'up') upCount++;
                else downCount++;
                const h = Math.max(10, (t.confidence || 50) * 0.8);
                return `<div class="pnl-bar ${dir}" style="height:${h}%" title="${t.direction} ${t.confidence || 0}%"></div>`;
            }).join('');

            document.getElementById('chartUpCount').textContent = `UP: ${upCount}`;
            document.getElementById('chartDownCount').textContent = `DOWN: ${downCount}`;'''
            
    new_pnl = '''            chart.innerHTML = last30.map(t => {
                const isWin = t.outcome === 'win';
                const isLoss = t.outcome === 'loss';
                const dir = isWin ? 'up' : (isLoss ? 'down' : (t.direction === 'UP' ? 'up' : 'down'));
                
                if (isWin) upCount++;
                else if (isLoss) downCount++;
                
                const h = Math.max(10, (t.confidence || 50) * 0.8);
                const opacity = (isWin || isLoss) ? '1' : '0.4';
                return `<div class="pnl-bar ${dir}" style="height:${h}%; opacity:${opacity}" title="${t.direction} ${t.confidence || 0}%"></div>`;
            }).join('');

            document.getElementById('chartUpCount').textContent = `WINS: ${upCount}`;
            document.getElementById('chartDownCount').textContent = `LOSS: ${downCount}`;'''
            
    if old_pnl in content:
        content = content.replace(old_pnl, new_pnl)
        
    # 6. Change property names in history render
    c1 = "const trades = data.trades || [];"
    c2 = "const trades = data.history || data.trades || [];"
    content = content.replace(c1, c2)

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(content)

if __name__ == "__main__":
    modify_dashboard()
