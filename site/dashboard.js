(function () {
  'use strict';

  var SIDEBAR_COLLAPSE_BREAKPOINT = 900; // px viewport width; see handoff notes for GAP-1 rationale
  var SCROLL_SPY_OFFSET = 16; // px, no fixed top nav on this page so a small lead-in is enough

  // ---- Pure functions -----------------------------------------------------
  // Kept free of DOM/window access so they can be exercised directly by a
  // future test harness (see handoff notes: SHELL-UNIT-001/002/003/006).

  function resolveActiveSection(sections, scrollY, offset, maxScrollY) {
    if (!sections || !sections.length) return null;
    // Once scrolled as far down as the page allows, the last section is
    // always "active" -- some pages don't leave enough room below their
    // final 1-2 sections for scrollY+offset to ever land inside them
    // (TASK-004-033/SHELL-UNIT-002), so the in-range/nearest-by-top scan
    // below would otherwise keep resolving to an earlier section forever.
    if (typeof maxScrollY === 'number' && scrollY >= maxScrollY) {
      return sections[sections.length - 1].id;
    }
    var y = scrollY + offset + 1;
    var candidate = null;
    var bestDist = Infinity;
    for (var i = 0; i < sections.length; i++) {
      var s = sections[i];
      if (y >= s.top && y < s.bottom) return s.id;
      var d = Math.abs(y - s.top);
      if (d < bestDist) {
        bestDist = d;
        candidate = s.id;
      }
    }
    return candidate;
  }

  function computeCollapseState(viewportWidth, breakpoint) {
    return viewportWidth < breakpoint ? 'collapsed' : 'expanded';
  }

  function formatStat(value) {
    if (value === null || value === undefined) return '—';
    if (typeof value === 'number' && isNaN(value)) return '—';
    return String(value);
  }

  function monthLabel(yyyyMm) {
    var MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    var parts = typeof yyyyMm === 'string' ? yyyyMm.split('-') : [];
    if (parts.length !== 2) return '—';
    var monthIndex = parseInt(parts[1], 10) - 1;
    if (isNaN(monthIndex) || monthIndex < 0 || monthIndex > 11) return '—';
    return MONTH_NAMES[monthIndex] + ' ' + parts[0];
  }

  function formatWindowLabel(win) {
    if (!win || !win.start || !win.end) return '—';
    return monthLabel(win.start) + ' – ' + monthLabel(win.end);
  }

  function formatCollected(generatedAt) {
    if (!generatedAt) return '—';
    return String(generatedAt).replace('T', ' ').replace('Z', ' UTC');
  }

  // ---- Team Overview / Activity chart: pure functions ----------------------
  // See handoff notes: OVERVIEW-UNIT-001..012. Kept free of DOM/Chart.js
  // instance access so they're independently testable.

  // 9 distinguishable hues for per-developer series, in roster order. Spec
  // §12 only lists 7 (emerald/sky/indigo/amber/orange/teal/violet) but the
  // roster is 9 — test-plan-overview.md GAP-3 flags this; resolved here by
  // extending with light-violet (distinct from the primary-violet H1/H2
  // divider line), rose, and lime, keeping all 9 legible on the dark canvas.
  var CHART_PALETTE_9 = ['#34d399', '#38bdf8', '#818cf8', '#fbbf24', '#fb923c', '#2dd4bf', '#a78bfa', '#fb7185', '#a3e635'];

  function formatCount(value) {
    if (value === null || value === undefined) return '—';
    if (typeof value === 'number' && isNaN(value)) return '—';
    return Number(value).toLocaleString('en-US');
  }

  function sumMonthlyField(monthly, field) {
    if (!Array.isArray(monthly)) return 0;
    return monthly.reduce(function (acc, row) {
      return acc + (row && typeof row[field] === 'number' ? row[field] : 0);
    }, 0);
  }

  function avgSkipNull(values) {
    var nums = (values || []).filter(function (v) { return typeof v === 'number' && !isNaN(v); });
    if (!nums.length) return null;
    return nums.reduce(function (a, b) { return a + b; }, 0) / nums.length;
  }

  function monthShortLabel(yyyyMm) {
    var full = monthLabel(yyyyMm);
    return full === '—' ? full : full.split(' ')[0];
  }

  // Boundary is derived from month-array length (H1 = first half, H2 = second
  // half) rather than a hardcoded pixel offset, per OVERVIEW-UNIT-007.
  // Returns the index of the LAST H1 month (5 for a 12-month window).
  function computeH1H2DividerIndex(monthCount) {
    if (!monthCount || monthCount < 2) return null;
    return Math.floor(monthCount / 2) - 1;
  }

  // mode: 'percent' (count metrics), 'days' (cycle time), 'point' (rate
  // stored as a 0..1 fraction, shown as percentage points), 'count' (raw
  // integer, e.g. active devs). lowerIsBetter flips which raw direction is
  // "good" (OVERVIEW-UNIT-010: a cycle-time decrease is good).
  function computeDelta(h1Value, h2Value, opts) {
    opts = opts || {};
    var mode = opts.mode || 'percent';
    var lowerIsBetter = !!opts.lowerIsBetter;
    var suffix = opts.suffix !== undefined ? opts.suffix : ' H2';
    // neutral: informational, non-scored metrics (e.g. Active Devs) never
    // get a judged up/down arrow, regardless of how much the raw value
    // moved -- always the unstyled steady dash (OVERVIEW-UX-004).
    if (opts.neutral) {
      return { steady: true, direction: null, good: null, text: '— steady' };
    }
    if (h1Value === null || h1Value === undefined || h2Value === null || h2Value === undefined) {
      return { steady: true, direction: null, good: null, text: '—' };
    }
    var diff = h2Value - h1Value;
    // A zero H1 baseline makes "percent change" mathematically undefined,
    // not "0%" -- the old code below fell back to a hardcoded pct of 0 in
    // this case, which made a genuine H2 increase from a 0 baseline (PRs
    // Merged / Reviews in the real fixture) misread as the zero-change
    // steady case a few lines down (TASK-004-033). If H2 is also 0 this is
    // a real tie and still falls through to that same shared steady check.
    if (mode === 'percent' && h1Value === 0 && diff !== 0) {
      var zeroBaseDirection = diff > 0 ? 'up' : 'down';
      var zeroBaseGood = lowerIsBetter ? diff < 0 : diff > 0;
      var zeroBaseArrow = diff > 0 ? '▲' : '▼';
      return { steady: false, direction: zeroBaseDirection, good: zeroBaseGood, text: zeroBaseArrow + ' New' + suffix };
    }
    var amountText;
    if (mode === 'percent') {
      var pct = h1Value !== 0 ? Math.abs(diff / h1Value) * 100 : 0;
      amountText = pct.toFixed(0) + '%';
    } else if (mode === 'point') {
      amountText = (Math.abs(diff) * 100).toFixed(1) + 'pt';
    } else if (mode === 'days') {
      amountText = Math.abs(diff).toFixed(1) + 'd';
    } else {
      amountText = formatCount(Math.abs(diff));
    }
    // Steady covers a literal H1===H2 tie AND a nonzero diff whose formatted
    // amount still reads as zero (e.g. percent mode can't express a % of an
    // H1 of 0, so it falls back to "0%" above) -- never fabricate an up/down
    // arrow around a value the tile displays as 0. One shared check for every
    // tile/mode instead of per-tile special-casing (OVERVIEW-UNIT-003).
    if (diff === 0 || parseFloat(amountText) === 0) {
      return { steady: true, direction: null, good: null, text: '— steady' };
    }
    var direction = diff > 0 ? 'up' : 'down';
    var good = lowerIsBetter ? diff < 0 : diff > 0;
    var arrow = diff > 0 ? '▲' : '▼';
    return { steady: false, direction: direction, good: good, text: arrow + ' ' + amountText + suffix };
  }

  var KPI_DEFS = [
    { key: 'commits', label: 'Commits', h1h2Key: 'commits_per_mo', mode: 'percent', lowerIsBetter: false, totalField: 'commits' },
    { key: 'prs', label: 'PRs Merged', h1h2Key: 'prs_per_mo', mode: 'percent', lowerIsBetter: false, totalField: 'prs_merged' },
    { key: 'reviews', label: 'Reviews', h1h2Key: 'reviews_per_mo', mode: 'percent', lowerIsBetter: false, totalField: 'reviews' },
    { key: 'active-devs', label: 'Active Devs', h1h2Key: 'active_devs', mode: 'count', neutral: true },
    { key: 'cycle', label: 'Avg Cycle', h1h2Key: 'cycle_time_days', mode: 'days', lowerIsBetter: true },
    { key: 'ci', label: 'CI Pass', h1h2Key: 'ci_pass_rate', mode: 'point', lowerIsBetter: false }
  ];

  // Renders exactly the 6 tiles above, in order (OVERVIEW-UNIT-001). Values
  // for count metrics are the full-window sum; cycle/CI are the 12-month
  // average (CI skips null months per OVERVIEW-UNIT-009); active devs shows
  // H2's count. Deltas always compare team.h1/team.h2 (OVERVIEW-UNIT-002/003).
  function buildKpiTiles(data) {
    var team = (data && data.team) || {};
    var monthly = team.monthly || [];
    var h1 = team.h1 || {};
    var h2 = team.h2 || {};

    return KPI_DEFS.map(function (def) {
      var value;
      if (def.key === 'active-devs') {
        value = formatCount(h2.active_devs !== undefined ? h2.active_devs : null);
      } else if (def.key === 'cycle') {
        var avgCycle = avgSkipNull(monthly.map(function (r) { return r.cycle_time_days; }));
        value = avgCycle === null ? '—' : avgCycle.toFixed(1) + 'd';
      } else if (def.key === 'ci') {
        var avgCi = avgSkipNull(monthly.map(function (r) { return r.ci_pass_rate; }));
        value = avgCi === null ? '—' : (avgCi * 100).toFixed(1) + '%';
      } else {
        value = formatCount(sumMonthlyField(monthly, def.totalField));
      }
      var delta = computeDelta(h1[def.h1h2Key], h2[def.h1h2Key], { mode: def.mode, lowerIsBetter: def.lowerIsBetter, neutral: def.neutral });
      return { key: def.key, label: def.label, value: value, delta: delta };
    });
  }

  function findMonthRow(monthly, month) {
    for (var i = 0; i < (monthly || []).length; i++) {
      if (monthly[i].month === month) return monthly[i];
    }
    return null;
  }

  // Team mode: 2 series (Commits, PRs Merged), colors matching mock.html's
  // literal SVG fills (OVERVIEW-UNIT-005/OVERVIEW-UX-002).
  function buildTeamActivityChartConfig(data) {
    var months = (data.window && data.window.months) || [];
    var monthly = (data.team && data.team.monthly) || [];
    var commits = months.map(function (m) {
      var row = findMonthRow(monthly, m);
      return row && typeof row.commits === 'number' ? row.commits : 0;
    });
    var prs = months.map(function (m) {
      var row = findMonthRow(monthly, m);
      return row && typeof row.prs_merged === 'number' ? row.prs_merged : 0;
    });
    return {
      labels: months.map(monthShortLabel),
      datasets: [
        { label: 'Commits', data: commits, backgroundColor: '#34d399', borderRadius: 3, maxBarThickness: 20 },
        { label: 'PRs Merged', data: prs, backgroundColor: '#38bdf8', borderRadius: 3, maxBarThickness: 20 }
      ]
    };
  }

  // Per-developer mode: exactly 9 series, one per roster entry, in roster
  // order (OVERVIEW-UNIT-006). Only roster handles are ever consulted, so a
  // stray bot/non-roster entry upstream in `developers` can never surface
  // (OVERVIEW-UNIT-012). Series value = monthly commits (the single clearest
  // activity volume metric — see handoff for why PRs weren't also split out).
  function buildPerDeveloperActivityChartConfig(data) {
    var months = (data.window && data.window.months) || [];
    var roster = data.roster || [];
    var developers = data.developers || [];
    var datasets = roster.map(function (person, idx) {
      var dev = null;
      for (var i = 0; i < developers.length; i++) {
        if (developers[i].handle === person.handle) { dev = developers[i]; break; }
      }
      var monthly = (dev && dev.monthly) || [];
      var series = months.map(function (m) {
        var row = findMonthRow(monthly, m);
        return row && typeof row.commits === 'number' ? row.commits : 0;
      });
      var color = CHART_PALETTE_9[idx % CHART_PALETTE_9.length];
      return {
        label: person.name, handle: person.handle, data: series,
        backgroundColor: color, borderColor: color, maxBarThickness: 9, borderRadius: 2
      };
    });
    return { labels: months.map(monthShortLabel), datasets: datasets };
  }

  function hexToRgba(hex, alpha) {
    var h = String(hex).replace('#', '');
    var r = parseInt(h.substring(0, 2), 16);
    var g = parseInt(h.substring(2, 4), 16);
    var b = parseInt(h.substring(4, 6), 16);
    return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
  }

  // Dims every dataset except the selected developer's (OVERVIEW-UNIT-011).
  // Pure — returns a new datasets array rather than mutating in place, so the
  // caller decides when/whether to push it into a live Chart.js instance.
  function applyDeveloperHighlight(datasets, selectedHandle) {
    return (datasets || []).map(function (ds) {
      var base = ds._baseColor || ds.borderColor || ds.backgroundColor;
      var dim = !!selectedHandle && !!ds.handle && ds.handle !== selectedHandle;
      var color = dim ? hexToRgba(base, 0.18) : base;
      var copy = {};
      for (var k in ds) { if (Object.prototype.hasOwnProperty.call(ds, k)) copy[k] = ds[k]; }
      copy._baseColor = base;
      copy.borderColor = color;
      copy.backgroundColor = color;
      return copy;
    });
  }

  // Chart.js plugin: dashed violet divider between month index 5 (Dec 2025)
  // and 6 (Jan 2026). Position comes from the x-scale's own pixel mapping
  // (not a stored offset), so it stays correct across resize/toggle.
  var h1h2DividerPlugin = {
    id: 'h1h2Divider',
    afterDraw: function (chart) {
      var months = (chart.data && chart.data.labels) || [];
      var idx = computeH1H2DividerIndex(months.length);
      if (idx === null || idx + 1 >= months.length) return;
      var scale = chart.scales && chart.scales.x;
      if (!scale) return;
      var x1 = scale.getPixelForValue(idx);
      var x2 = scale.getPixelForValue(idx + 1);
      var x = (x1 + x2) / 2;
      var area = chart.chartArea;
      var ctx = chart.ctx;
      ctx.save();
      ctx.beginPath();
      ctx.setLineDash([4, 4]);
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = '#a78bfa';
      ctx.moveTo(x, area.top);
      ctx.lineTo(x, area.bottom);
      ctx.stroke();
      ctx.restore();

      ctx.save();
      ctx.font = '11px Inter, sans-serif';
      ctx.fillStyle = '#a78bfa';
      ctx.textBaseline = 'top';
      ctx.textAlign = 'right';
      ctx.fillText('H1', x - 6, area.top);
      ctx.textAlign = 'left';
      ctx.fillText('H2', x + 6, area.top);
      ctx.restore();
    }
  };


  // ---- Developer Scorecards: pure functions --------------------------------
  // Composite scores/signals are precomputed collector-side (scoring.py) and
  // read verbatim here; this module only formats/joins for display, never
  // recomputes the formula (single source of truth stays the collector).
  // See handoff notes: DEVPANEL-UNIT-001/002/007.

  function buildScorecardFormulaLabel(weights) {
    weights = weights || {};
    var order = [['commits', 'commits'], ['prs', 'PRs'], ['reviews', 'reviews'], ['tests', 'tests'], ['ci', 'CI']];
    var parts = order.map(function (pair) {
      var w = typeof weights[pair[0]] === 'number' ? weights[pair[0]] : 0;
      return w.toFixed(2) + ' ' + pair[1];
    });
    return 'Developer Scorecards · composite = ' + parts.join(' + ');
  }

  function buildScorecardViewModels(data) {
    var roster = data.roster || [];
    var developers = data.developers || [];
    var months = (data.window && data.window.months) || [];
    return roster.map(function (person) {
      var dev = null;
      for (var i = 0; i < developers.length; i++) {
        if (developers[i].handle === person.handle) { dev = developers[i]; break; }
      }
      var composite = (dev && dev.composite) || { score: 0, signals: {} };
      var monthly = (dev && dev.monthly) || [];
      var sparkline = months.map(function (m) {
        var row = null;
        for (var j = 0; j < monthly.length; j++) { if (monthly[j].month === m) { row = monthly[j]; break; } }
        return row && typeof row.composite === 'number' ? row.composite : 0;
      });
      // Per-month breakdown for the monthly bar chart (commits + PRs)
      var monthlyBreakdown = months.map(function (m) {
        var row = null;
        for (var j = 0; j < monthly.length; j++) { if (monthly[j].month === m) { row = monthly[j]; break; } }
        return {
          month: m,
          commits: row && typeof row.commits === 'number' ? row.commits : 0,
          prs: row && typeof row.prs_merged === 'number' ? row.prs_merged : 0,
          reviews: row && typeof row.reviews_given === 'number' ? row.reviews_given : 0
        };
      });
      // Raw totals for signal bars
      var rawTotals = {
        commits: monthly.reduce(function (s, r) { return s + (typeof r.commits === 'number' ? r.commits : 0); }, 0),
        prs: monthly.reduce(function (s, r) { return s + (typeof r.prs_merged === 'number' ? r.prs_merged : 0); }, 0),
        reviews: monthly.reduce(function (s, r) { return s + (typeof r.reviews_given === 'number' ? r.reviews_given : 0); }, 0)
      };
      return {
        name: person.name, handle: person.handle, initials: person.initials,
        score: composite.score || 0, signals: composite.signals || {}, sparkline: sparkline,
        monthlyBreakdown: monthlyBreakdown, rawTotals: rawTotals
      };
    });
  }

  // 5-signal color sequence per DEVPANEL-UX-B-003: emerald/sky/indigo/amber/teal
  var SIGNAL_ORDER = [
    { key: 'commits', label: 'Commits', color: '#34d399' },
    { key: 'prs', label: 'PRs', color: '#38bdf8' },
    { key: 'reviews', label: 'Reviews', color: '#818cf8' },
    { key: 'tests', label: 'Tests', color: '#fbbf24' },
    { key: 'ci', label: 'CI', color: '#2dd4bf' }
  ];

  // null/undefined/NaN -> 0-height bar (never a crash on a zero-activity
  // month, DEVPANEL-E2E-004), never negative or >100 (DEVPANEL-UNIT-007).
  function formatSignalBarHeight(value) {
    if (value === null || value === undefined || (typeof value === 'number' && isNaN(value))) return 0;
    return Math.max(0, Math.min(100, value * 100));
  }

  // null/undefined/NaN -> '—' not '0%' (DEVPANEL-UNIT-003's honesty rule).
  function formatSignalValue(value) {
    if (value === null || value === undefined || (typeof value === 'number' && isNaN(value))) return '—';
    return Math.round(value * 100) + '%';
  }

  // GAP-1: small SVG polyline sparkline, fixed 0..domainMax domain (composite
  // is 0..100) so cards are visually comparable rather than each auto-scaling.
  function buildSparklinePoints(values, width, height, domainMax) {
    domainMax = domainMax || 100;
    var vals = values || [];
    if (!vals.length) return '';
    var stepX = vals.length > 1 ? width / (vals.length - 1) : 0;
    return vals.map(function (v, i) {
      var x = i * stepX;
      var clamped = Math.max(0, Math.min(domainMax, typeof v === 'number' && !isNaN(v) ? v : 0));
      var y = height - (clamped / domainMax) * height;
      return x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');
  }

  // ---- Reviews panel: pure functions ----------------------------------------
  // See handoff notes: DEVPANEL-UNIT-006, DEVPANEL-INT-002.

  // Fixed per-row-rank color sequence, matches mock.html's literal bar colors
  // (assigned by descending-share rank, not by developer identity).
  var REVIEW_BAR_COLORS = ['#7c3aed', '#818cf8', '#38bdf8', '#34d399', '#2dd4bf', '#fbbf24', '#fb923c', '#94a3b8', '#64748b'];

  function buildReviewShareRows(data) {
    var roster = data.roster || [];
    var developers = data.developers || [];
    var rows = roster.map(function (person) {
      var dev = null;
      for (var i = 0; i < developers.length; i++) {
        if (developers[i].handle === person.handle) { dev = developers[i]; break; }
      }
      var share = dev && typeof dev.review_share === 'number' ? dev.review_share : 0;
      return { name: person.name, handle: person.handle, share: share };
    });
    rows.sort(function (a, b) { return b.share - a.share; });
    return rows.map(function (row, idx) {
      return {
        name: row.name, handle: row.handle, share: row.share,
        pct: Math.round(row.share * 100),
        color: REVIEW_BAR_COLORS[idx % REVIEW_BAR_COLORS.length]
      };
    });
  }

  // Activity-weighted average, matching bucketing.py's own roll-up convention
  // (a developer's busier month/repo contributes proportionally more than an
  // idle one) rather than a plain unweighted mean across rows.
  function weightedAvg(pairs) {
    var num = 0, den = 0;
    (pairs || []).forEach(function (p) {
      if (typeof p.value === 'number' && !isNaN(p.value) && typeof p.weight === 'number' && p.weight > 0) {
        num += p.value * p.weight;
        den += p.weight;
      }
    });
    return den > 0 ? num / den : null;
  }

  function computeReviewAggregateStats(data) {
    var developers = data.developers || [];
    var turnaroundPairs = [];
    var crPairs = [];
    developers.forEach(function (dev) {
      (dev.monthly || []).forEach(function (row) {
        turnaroundPairs.push({ value: row.review_turnaround_hours, weight: row.reviews_given || 0 });
        crPairs.push({ value: row.change_request_rate, weight: row.prs_merged || 0 });
      });
    });
    return {
      avgTurnaroundHours: weightedAvg(turnaroundPairs),
      changeRequestRate: weightedAvg(crPairs)
    };
  }

  // ---- DORA panel: pure functions --------------------------------------------
  // See handoff notes: DEVPANEL-UNIT-004, DEVPANEL-E2E-007. MTTR's displayed
  // value is a hardcoded 'N/A' literal below, never read from team.dora.mttr,
  // so no upstream data change can ever cause a fabricated MTTR number.
  function buildDoraBoxes(data) {
    var dora = (data.team && data.team.dora) || {};
    var deployFreq = typeof dora.deploy_frequency_per_month === 'number' ? dora.deploy_frequency_per_month.toFixed(1) + '/mo' : '—';
    var leadTime = typeof dora.lead_time_days === 'number' ? dora.lead_time_days.toFixed(1) + 'd' : '—';
    var changeFailure = typeof dora.change_failure_rate === 'number' ? (dora.change_failure_rate * 100).toFixed(1) + '%' : '—';
    return [
      { key: 'deploy', label: 'Deploy frequency', value: deployFreq, color: '#34d399', sub: 'PRs merged to default branch' },
      { key: 'lead', label: 'Lead time', value: leadTime, color: '#38bdf8', sub: 'merged − created' },
      { key: 'cf', label: 'Change-failure', value: changeFailure, color: '#fb923c', sub: 'title-keyword heuristic' },
      { key: 'mttr', label: 'MTTR', value: 'N/A', color: '#64748b', sub: 'no incident source — not faked' }
    ];
  }

  // ---- Per-Repo Breakdown: pure functions -----------------------------------
  // See handoff notes "Schema decisions" (REPOFILTER GAP-2): `repo_breakdown`
  // is repo-level only, so the cross-panel developer highlight instead reads
  // a per-developer `active_repos` list extended into the fixture the same
  // additive way TASK-004-012 added reviews_per_mo/active_devs to team.h1/h2.

  function formatCiPassRate(value) {
    return formatSignalValue(value);
  }

  // "Activity volume" = commits+prs+reviews, so a tie in one field still
  // resolves sensibly. Matches mock.html's repo order, which tracks commits
  // descending 1:1 on our fixture's numbers.
  function repoActivityScore(repo) {
    if (!repo) return 0;
    var commits = typeof repo.commits === 'number' ? repo.commits : 0;
    var prs = typeof repo.prs === 'number' ? repo.prs : 0;
    var reviews = typeof repo.reviews === 'number' ? repo.reviews : 0;
    return commits + prs + reviews;
  }

  // Active-first, stable sort (REPOFILTER-UNIT-002). Returns a new array —
  // never mutates or drops a repo, even an all-zero one (REPOFILTER-UNIT-007).
  function sortRepoBreakdown(repos) {
    return (repos || [])
      .map(function (r, i) { return { r: r, i: i }; })
      .sort(function (a, b) {
        var diff = repoActivityScore(b.r) - repoActivityScore(a.r);
        return diff !== 0 ? diff : a.i - b.i;
      })
      .map(function (pair) { return pair.r; });
  }

  // A repo is "low-activity" once fewer than half the roster has ever touched
  // it (REPOFILTER-UNIT-003) — roster-relative rather than a magic number so
  // it stays correct as the team grows/shrinks.
  function isLowActivityRepo(repo, rosterSize) {
    var activeDevs = repo && typeof repo.active_devs === 'number' ? repo.active_devs : 0;
    return activeDevs < (rosterSize || 0) / 2;
  }

  function buildRepoTableRows(data) {
    var rosterSize = Array.isArray(data.roster) ? data.roster.length : 0;
    return sortRepoBreakdown(data.repo_breakdown).map(function (repo) {
      return {
        repo: repo.repo,
        commits: formatCount(repo.commits),
        prs: formatCount(repo.prs),
        reviews: formatCount(repo.reviews),
        ciPass: formatCiPassRate(repo.ci_pass_rate),
        activeDevs: formatCount(repo.active_devs),
        muted: isLowActivityRepo(repo, rosterSize)
      };
    });
  }

  function findDeveloperByHandle(data, handle) {
    if (!handle) return null;
    var developers = data.developers || [];
    for (var i = 0; i < developers.length; i++) {
      if (developers[i].handle === handle) return developers[i];
    }
    return null;
  }

  // Developer -> active-repo lookup for the cross-panel highlight
  // (REPOFILTER-UNIT-008). null means "no developer selected"; [] means
  // "selected, but no per-repo activity" — callers can tell these apart.
  function findDeveloperActiveRepos(data, handle) {
    if (!handle) return null;
    var dev = findDeveloperByHandle(data, handle);
    return dev && Array.isArray(dev.active_repos) ? dev.active_repos : [];
  }

  // ---- H1 vs H2 comparison: pure functions -----------------------------------
  // Card set mirrors mock.html's 4-card grid exactly (Commits/PRs/Cycle-time/
  // CI — no Reviews or Active-Devs card).

  // `devKey` is set only for metrics the per-developer h1/h2 object actually
  // carries (collector/persist.py's `_developer_half` emits just `commits`
  // and `composite` -- see test_qa_independent_h1h2.py's locked-in schema
  // assertions, so that shape is not something dashboard.js can change).
  // Metrics with no `devKey` fall back to the team h1/h2 figures when a
  // developer is selected (buildH1H2Cards), labeled "(team)" so a team-wide
  // number is never misread as that individual's (REPOFILTER GAP-3, option B).
  var H1H2_DEFS = [
    { key: 'commits', label: 'Commits / mo', h1h2Key: 'commits_per_mo', devKey: 'commits', mode: 'percent', lowerIsBetter: false, format: 'count' },
    { key: 'prs', label: 'PRs / mo', h1h2Key: 'prs_per_mo', mode: 'percent', lowerIsBetter: false, format: 'count' },
    { key: 'cycle', label: 'Cycle time', h1h2Key: 'cycle_time_days', mode: 'days', lowerIsBetter: true, format: 'days' },
    { key: 'ci', label: 'CI pass', h1h2Key: 'ci_pass_rate', mode: 'point', lowerIsBetter: false, format: 'point' }
  ];

  function formatH1H2Value(value, format) {
    if (value === null || value === undefined || (typeof value === 'number' && isNaN(value))) return '—';
    if (format === 'days') return value.toFixed(1) + 'd';
    if (format === 'point') return Math.round(value * 100) + '%';
    return formatCount(Math.round(value));
  }

  // Builds the 4 cards from either team.h1/h2 (no developer selected) or a
  // single developer's h1/h2 (selected). The developer object only carries
  // `commits`/`composite` (def.devKey) — collector/persist.py's
  // `_developer_half` locked that 2-key shape in (see
  // test_qa_independent_h1h2.py), it does not carry commits_per_mo/
  // prs_per_mo/cycle_time_days/ci_pass_rate the way team.h1/h2 does. When
  // `teamH1`/`teamH2` are passed (a developer is selected) and a def has no
  // devKey, that card falls back to the team figures with a " (team)" label
  // suffix so a team-wide number is never misread as that developer's own
  // (TASK-004-038 fix, REPOFILTER GAP-3 option B). Passing no teamH1/teamH2
  // (team view) disables the fallback — h1/h2 already are the team object.
  // Cycle-time's decrease still renders as an improvement (REPOFILTER-UNIT-005)
  // purely because computeDelta's lowerIsBetter flips direction — this
  // function does nothing sign-specific.
  // Card deltas read "▲ 38%", not "▲ 38% H2" (REPOFILTER-UNIT-004/006) since
  // both halves are already named on the value line ("340 → 470").
  function buildH1H2Cards(h1, h2, teamH1, teamH2) {
    h1 = h1 || {};
    h2 = h2 || {};
    // teamH1/teamH2 are only passed when a developer is selected (renderH1H2) --
    // that's also the only time it's correct to read def.devKey off h1/h2
    // instead of def.h1h2Key, since in team view h1/h2 already ARE the team
    // object and only have h1h2Key (e.g. commits_per_mo), never devKey (commits).
    var devScoped = !!(teamH1 && teamH2);
    return H1H2_DEFS.map(function (def) {
      var sourceKey = (devScoped && def.devKey) ? def.devKey : def.h1h2Key;
      var v1 = h1[sourceKey];
      var v2 = h2[sourceKey];
      var label = def.label;
      if (devScoped && !def.devKey) {
        v1 = teamH1[def.h1h2Key];
        v2 = teamH2[def.h1h2Key];
        label = label + ' (team)';
      }
      return {
        key: def.key,
        label: label,
        text: formatH1H2Value(v1, def.format) + ' → ' + formatH1H2Value(v2, def.format),
        delta: computeDelta(v1, v2, { mode: def.mode, lowerIsBetter: def.lowerIsBetter, suffix: '' })
      };
    });
  }

  // ---- DOM wiring -----------------------------------------------------------

  function readMetricsData() {
    var el = document.getElementById('metrics-data');
    if (!el) return {};
    try {
      return JSON.parse(el.textContent || '{}');
    } catch (err) {
      console.error('dashboard: could not parse #metrics-data', err);
      return {};
    }
  }

  function setText(id, value) {
    var el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  function renderHeader(data) {
    var rosterCount = Array.isArray(data.roster) ? data.roster.length : null;
    var repoCount = Array.isArray(data.repos) ? data.repos.length : null;
    var monthCount = data.window && Array.isArray(data.window.months) ? data.window.months.length : null;

    setText('statRoster', formatStat(rosterCount));
    setText('statRepos', formatStat(repoCount));
    setText('statMonths', formatStat(monthCount));
    setText('sidebarWindow', formatWindowLabel(data.window));
    setText('sidebarCollected', formatCollected(data.generated_at));
  }

  function renderKpiRow(data) {
    var idSuffix = { commits: 'Commits', prs: 'Prs', reviews: 'Reviews', 'active-devs': 'ActiveDevs', cycle: 'Cycle', ci: 'Ci' };
    buildKpiTiles(data).forEach(function (tile) {
      var suffix = idSuffix[tile.key];
      setText('kpi' + suffix + 'Value', tile.value);
      var deltaEl = document.getElementById('kpi' + suffix + 'Delta');
      if (!deltaEl) return;
      deltaEl.textContent = tile.delta.text;
      deltaEl.classList.remove('up', 'down', 'steady');
      deltaEl.classList.add(tile.delta.steady ? 'steady' : (tile.delta.good ? 'up' : 'down'));
    });
  }

  // Chart.js plugin: renders the numeric value on top of each bar in the
  // team activity chart, so the raw numbers are always visible (not just
  // the visual bar height).  Only draws for non-zero values.
  var barDataLabelsPlugin = {
    id: 'barDataLabels',
    afterDatasetsDraw: function (chart) {
      var ctx = chart.ctx;
      ctx.save();
      ctx.font = '10px JetBrains Mono';
      ctx.textAlign = 'center';
      ctx.fillStyle = '#94a3b8';
      chart.data.datasets.forEach(function (ds, di) {
        var meta = chart.getDatasetMeta(di);
        if (meta.hidden) return;
        meta.data.forEach(function (bar, bi) {
          var val = ds.data[bi];
          if (typeof val !== 'number' || val === 0) return;
          // Only label bars that are tall enough to not overlap the legend
          ctx.fillText(String(val), bar.x, bar.y - 4);
        });
      });
      ctx.restore();
    }
  };

  function activityChartOptions() {
    var axisTicks = { color: '#64748b', font: { family: 'JetBrains Mono', size: 11 } };
    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { grid: { color: 'rgba(255,255,255,.06)' }, ticks: axisTicks },
        y: { beginAtZero: true, grid: { color: 'rgba(255,255,255,.06)' }, ticks: axisTicks }
      },
      plugins: {
        legend: { position: 'top', labels: { color: '#94a3b8', boxWidth: 10, font: { size: 11.5 } } },
        tooltip: { backgroundColor: '#1e293b', borderColor: 'rgba(255,255,255,.1)', borderWidth: 1, titleColor: '#fff', bodyColor: '#cbd5e1' }
      }
    };
  }

  var activityChart = { instance: null, mode: 'team', data: null };

  function initActivityChart(data) {
    var canvas = document.getElementById('activityChart');
    if (!canvas || typeof Chart === 'undefined') return;
    activityChart.data = data;
    var config = buildTeamActivityChartConfig(data);
    activityChart.instance = new Chart(canvas.getContext('2d'), {
      type: 'bar', data: config, options: activityChartOptions(), plugins: [h1h2DividerPlugin, barDataLabelsPlugin]
    });

    var toggle = document.getElementById('activityToggle');
    if (!toggle) return;
    toggle.addEventListener('click', function () {
      activityChart.mode = activityChart.mode === 'team' ? 'developer' : 'team';
      toggle.setAttribute('aria-pressed', activityChart.mode === 'developer' ? 'true' : 'false');
      toggle.querySelector('.mode-team').classList.toggle('active', activityChart.mode === 'team');
      toggle.querySelector('.mode-dev').classList.toggle('active', activityChart.mode === 'developer');
      var next = activityChart.mode === 'team'
        ? buildTeamActivityChartConfig(activityChart.data)
        : buildPerDeveloperActivityChartConfig(activityChart.data);
      activityChart.instance.data.labels = next.labels;
      activityChart.instance.data.datasets = next.datasets;
      activityChart.instance.update();
    });

    // Cross-panel developer-filter contract for a future panel (per spec
    // §11's page-wide developer filter, built in a later task) to drive this
    // chart's highlight without either side needing to know about the other's
    // internals: `window.dispatchEvent(new CustomEvent('dashboard:developer-filter', {detail:{handle}}))`.
    window.addEventListener('dashboard:developer-filter', function (evt) {
      if (activityChart.mode !== 'developer' || !activityChart.instance) return;
      var handle = evt && evt.detail ? evt.detail.handle : null;
      activityChart.instance.data.datasets = applyDeveloperHighlight(activityChart.instance.data.datasets, handle);
      activityChart.instance.update();
    });
  }

  function initScrollSpy() {
    var links = Array.prototype.slice.call(document.querySelectorAll('.sidebar-link'));
    var sections = [];

    links.forEach(function (link) {
      var href = link.getAttribute('href') || '';
      if (href.charAt(0) !== '#') return;
      var target = document.getElementById(href.slice(1));
      if (!target) return; // defensive: nav link with no matching section is simply skipped
      sections.push({ id: href.slice(1), link: link, top: 0, bottom: 0 });
    });

    if (!sections.length) return;

    function measure() {
      sections.forEach(function (s) {
        var el = document.getElementById(s.id);
        s.top = el.offsetTop;
        s.bottom = s.top + el.offsetHeight;
      });
    }

    function setActive(activeId) {
      sections.forEach(function (s) {
        var isActive = s.id === activeId;
        s.link.classList.toggle('active', isActive);
        s.link.setAttribute('aria-current', isActive ? 'true' : 'false');
      });
    }

    // Held (and kept alive by armSettleTimer while scrolling continues)
    // during a nav click's scrollIntoView animation, so the offset-based
    // resolver below can't clobber the clicked link's highlight before the
    // page truly finishes scrolling -- needed because on this page the last
    // 1-2 sections can't be scrolled far enough to satisfy the offset check
    // at all (TASK-004-033: no room left below them to keep scrolling), so
    // without this a click on one of them would settle back to whichever
    // section resolveActiveSection resolves to instead of the one clicked.
    var programmaticScrollId = null;
    var settleTimer = null;
    function armSettleTimer() {
      if (settleTimer) window.clearTimeout(settleTimer);
      settleTimer = window.setTimeout(function () { programmaticScrollId = null; }, 150);
    }

    function update() {
      var maxScrollY = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
      setActive(resolveActiveSection(sections, window.scrollY, SCROLL_SPY_OFFSET, maxScrollY));
    }

    var ticking = false;
    function onScroll() {
      if (programmaticScrollId) armSettleTimer();
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(function () {
        if (!programmaticScrollId) update();
        ticking = false;
      });
    }

    measure();
    update();
    window.addEventListener('resize', function () {
      measure();
      update();
    });
    window.addEventListener('scroll', onScroll, { passive: true });

    links.forEach(function (link) {
      link.addEventListener('click', function (e) {
        var href = link.getAttribute('href') || '';
        if (href.charAt(0) !== '#') return;
        var target = document.getElementById(href.slice(1));
        if (!target) return;
        e.preventDefault();
        programmaticScrollId = href.slice(1);
        setActive(programmaticScrollId);
        armSettleTimer();
        target.scrollIntoView({ behavior: 'smooth' });
      });
    });
  }

  function initSidebarCollapse() {
    var sidebar = document.getElementById('sidebar');
    if (!sidebar) return;

    function apply() {
      var state = computeCollapseState(window.innerWidth, SIDEBAR_COLLAPSE_BREAKPOINT);
      sidebar.classList.toggle('collapsed', state === 'collapsed');
      sidebar.classList.toggle('expanded', state === 'expanded');
      // Mirrored on <body> so `.content`'s margin-left (a sibling of
      // `.sidebar`, so it can't inherit `.sidebar`'s own custom property)
      // tracks the same --sidebar-width value. See dashboard.css.
      document.body.classList.toggle('sidebar-collapsed', state === 'collapsed');
      document.body.classList.toggle('sidebar-expanded', state === 'expanded');
    }

    apply();
    window.addEventListener('resize', apply);
  }


  // ---- Developer Scorecards / Reviews / DORA: DOM wiring ---------------------
  // Cross-panel developer filter: reuses the same 'dashboard:developer-filter'
  // CustomEvent contract TASK-004-012's activity chart already listens for
  // (window.dispatchEvent(new CustomEvent(..., {detail:{handle}}))). This
  // module both dispatches it (clicking a scorecard/review row, DEVPANEL-E2E-002)
  // and listens for it (so a future global filter control, or another panel's
  // click, drives the same highlight here) — single listener, any dispatcher.

  var developerFilter = { selected: null };

  function applyDeveloperFilterHighlight(handle) {
    var cards = document.querySelectorAll('.scorecard');
    for (var i = 0; i < cards.length; i++) {
      var card = cards[i];
      var isSelected = !!handle && card.getAttribute('data-handle') === handle;
      card.classList.toggle('selected', isSelected);
      card.classList.toggle('dimmed', !!handle && !isSelected);
    }
    var rows = document.querySelectorAll('.review-row');
    for (var j = 0; j < rows.length; j++) {
      var row = rows[j];
      var rowSelected = !!handle && row.getAttribute('data-handle') === handle;
      row.classList.toggle('selected', rowSelected);
      row.classList.toggle('dimmed', !!handle && !rowSelected);
    }
    // DORA panel is intentionally left untouched (GAP-2): it's team-level
    // only, no per-developer breakdown exists to highlight against.
  }

  function toggleDeveloperFilter(handle) {
    developerFilter.selected = developerFilter.selected === handle ? null : handle;
    window.dispatchEvent(new CustomEvent('dashboard:developer-filter', { detail: { handle: developerFilter.selected } }));
  }

  window.addEventListener('dashboard:developer-filter', function (evt) {
    applyDeveloperFilterHighlight(evt && evt.detail ? evt.detail.handle : null);
  });

  // Single shared tooltip element for signal-bar hover (DEVPANEL-E2E-008):
  // a real DOM element (not the native `title` attribute) so it's directly
  // queryable/assertable by a Playwright test, shown/hidden on hover/mouseout.
  var barTooltipEl = null;
  function showBarTooltip(evt, text) {
    if (!barTooltipEl) {
      barTooltipEl = document.createElement('div');
      barTooltipEl.className = 'bar-tooltip';
      document.body.appendChild(barTooltipEl);
    }
    barTooltipEl.textContent = text;
    barTooltipEl.style.left = (evt.clientX + 12) + 'px';
    barTooltipEl.style.top = (evt.clientY + 12) + 'px';
    barTooltipEl.style.display = 'block';
  }
  function hideBarTooltip() {
    if (barTooltipEl) barTooltipEl.style.display = 'none';
  }

  function renderScorecards(data) {
    var heading = document.getElementById('scorecardsHeading');
    if (heading) heading.textContent = buildScorecardFormulaLabel(data.score_weights);
    var grid = document.getElementById('scorecardGrid');
    if (!grid) return;
    grid.innerHTML = '';
    buildScorecardViewModels(data).forEach(function (vm) {
      grid.appendChild(renderScorecardCard(vm));
    });
  }

  // monthShortLabel already defined above (line ~95); the duplicate below
  // in renderScorecardCard has been removed to avoid shadowing.

  function renderScorecardCard(vm) {
    var card = document.createElement('div');
    card.className = 'card scorecard';
    card.setAttribute('data-handle', vm.handle);

    var head = document.createElement('div');
    head.className = 'scorecard-head';
    var badge = document.createElement('div');
    badge.className = 'avatar-badge';
    badge.textContent = vm.initials;
    var nameWrap = document.createElement('div');
    var nameEl = document.createElement('div');
    nameEl.className = 'scorecard-name';
    nameEl.textContent = vm.name;
    var handleEl = document.createElement('div');
    handleEl.className = 'scorecard-handle mono';
    handleEl.textContent = '@' + vm.handle;
    nameWrap.appendChild(nameEl);
    nameWrap.appendChild(handleEl);
    var compositeWrap = document.createElement('div');
    compositeWrap.className = 'scorecard-composite';
    var compositeVal = document.createElement('div');
    compositeVal.className = 'scorecard-composite-val mono';
    compositeVal.textContent = Math.round(vm.score);
    var compositeLab = document.createElement('div');
    compositeLab.className = 'scorecard-composite-lab';
    compositeLab.textContent = 'COMPOSITE';
    compositeWrap.appendChild(compositeVal);
    compositeWrap.appendChild(compositeLab);
    head.appendChild(badge);
    head.appendChild(nameWrap);
    head.appendChild(compositeWrap);

    // Monthly bar chart: commits (emerald) + PRs (sky) per month with grid + labels
    var monthlyWrap = document.createElement('div');
    monthlyWrap.className = 'scorecard-monthly';
    var monthlyLabel = document.createElement('div');
    monthlyLabel.className = 'scorecard-monthly-label';
    monthlyLabel.textContent = 'Monthly Output';
    monthlyWrap.appendChild(monthlyLabel);

    var mb = vm.monthlyBreakdown || [];
    var maxCommits = Math.max.apply(null, mb.map(function (r) { return r.commits; }).concat([1]));
    var maxPrs = Math.max.apply(null, mb.map(function (r) { return r.prs; }).concat([1]));
    var maxVal = Math.max(maxCommits, maxPrs);
    // Round up to a nice number for grid lines
    var niceMax = Math.ceil(maxVal / 10) * 10;
    if (niceMax < 10) niceMax = 10;

    // Chart area: y-axis labels on the left, bars + grid on the right
    var chartArea = document.createElement('div');
    chartArea.className = 'scorecard-monthly-chart';

    // Y-axis labels
    var yAxis = document.createElement('div');
    yAxis.className = 'scorecard-monthly-yaxis';
    var gridSteps = 4; // 0, 25%, 50%, 75%, 100%
    for (var gi = gridSteps; gi >= 0; gi--) {
      var yLabel = document.createElement('div');
      yLabel.className = 'scorecard-monthly-ylabel';
      yLabel.textContent = Math.round(niceMax * gi / gridSteps);
      yAxis.appendChild(yLabel);
    }

    // Bars container with grid lines behind
    var barsArea = document.createElement('div');
    barsArea.className = 'scorecard-monthly-barsarea';

    // Horizontal grid lines (absolutely positioned behind bars)
    for (var gl = 0; gl <= gridSteps; gl++) {
      var gridLine = document.createElement('div');
      gridLine.className = 'scorecard-monthly-gridline';
      gridLine.style.bottom = (gl / gridSteps * 100) + '%';
      barsArea.appendChild(gridLine);
    }

    var barsRow = document.createElement('div');
    barsRow.className = 'scorecard-monthly-barsrow';

    mb.forEach(function (r) {
      var monthCol = document.createElement('div');
      monthCol.className = 'scorecard-month-col';

      var barsContainer = document.createElement('div');
      barsContainer.className = 'scorecard-month-bars';

      // Commits bar
      var commitBar = document.createElement('div');
      commitBar.className = 'scorecard-month-bar-commits';
      var commitH = niceMax > 0 ? (r.commits / niceMax) * 100 : 0;
      commitBar.style.height = commitH + '%';
      commitBar.style.background = '#34d399';
      commitBar.style.width = '38%';
      commitBar.style.borderRadius = '2px 2px 0 0';
      commitBar.style.position = 'absolute';
      commitBar.style.bottom = '0';
      commitBar.style.left = '12%';

      // Commits value label
      if (r.commits > 0) {
        var commitLabel = document.createElement('div');
        commitLabel.className = 'scorecard-month-value';
        commitLabel.textContent = r.commits;
        commitLabel.style.bottom = 'calc(' + commitH + '% + 2px)';
        commitLabel.style.left = '12%';
        commitLabel.style.width = '38%';
        barsContainer.appendChild(commitLabel);
      }

      // PRs bar
      var prBar = document.createElement('div');
      prBar.className = 'scorecard-month-bar-prs';
      var prH = niceMax > 0 ? (r.prs / niceMax) * 100 : 0;
      prBar.style.height = prH + '%';
      prBar.style.background = '#38bdf8';
      prBar.style.width = '38%';
      prBar.style.borderRadius = '2px 2px 0 0';
      prBar.style.position = 'absolute';
      prBar.style.bottom = '0';
      prBar.style.left = '50%';

      // PRs value label
      if (r.prs > 0) {
        var prLabel = document.createElement('div');
        prLabel.className = 'scorecard-month-value';
        prLabel.textContent = r.prs;
        prLabel.style.bottom = 'calc(' + prH + '% + 2px)';
        prLabel.style.left = '50%';
        prLabel.style.width = '38%';
        prLabel.style.color = '#38bdf8';
        barsContainer.appendChild(prLabel);
      }

      barsContainer.appendChild(commitBar);
      barsContainer.appendChild(prBar);

      // Tooltip on hover
      var tipText = monthShortLabel(r.month) + ': ' + r.commits + ' commits, ' + r.prs + ' PRs, ' + r.reviews + ' reviews';
      barsContainer.addEventListener('mouseenter', function (evt) { showBarTooltip(evt, tipText); });
      barsContainer.addEventListener('mousemove', function (evt) { showBarTooltip(evt, tipText); });
      barsContainer.addEventListener('mouseleave', hideBarTooltip);

      var monthLabel = document.createElement('div');
      monthLabel.className = 'scorecard-month-label';
      monthLabel.textContent = monthShortLabel(r.month);

      monthCol.appendChild(barsContainer);
      monthCol.appendChild(monthLabel);
      barsRow.appendChild(monthCol);
    });

    barsArea.appendChild(barsRow);
    chartArea.appendChild(yAxis);
    chartArea.appendChild(barsArea);
    monthlyWrap.appendChild(chartArea);

    // Legend for the monthly chart
    var legend = document.createElement('div');
    legend.className = 'scorecard-monthly-legend';
    legend.innerHTML = '<span style="color:#34d399">●</span> Commits <span style="color:#38bdf8;margin-left:8px">●</span> PRs';
    monthlyWrap.appendChild(legend);

    // Signal bars with raw numbers
    var bars = document.createElement('div');
    bars.className = 'signal-bars';
    SIGNAL_ORDER.forEach(function (sig) {
      var value = vm.signals[sig.key];
      var rawValue = vm.rawTotals ? vm.rawTotals[sig.key] : null;
      var col = document.createElement('div');
      col.className = 'signal-bar-col';
      var track = document.createElement('div');
      track.className = 'signal-bar-track';
      var fill = document.createElement('div');
      fill.className = 'signal-bar-fill';
      fill.style.height = formatSignalBarHeight(value) + '%';
      fill.style.background = sig.color;
      // Tooltip shows both percentage and raw number
      var tipText = sig.label + ': ' + formatSignalValue(value) +
        (rawValue !== null && rawValue !== undefined ? ' (' + rawValue + ' total)' : '');
      track.addEventListener('mouseenter', function (evt) { showBarTooltip(evt, tipText); });
      track.addEventListener('mousemove', function (evt) { showBarTooltip(evt, tipText); });
      track.addEventListener('mouseleave', hideBarTooltip);
      track.appendChild(fill);
      var label = document.createElement('div');
      label.className = 'signal-bar-label';
      // Show raw number below the label for commits, PRs, reviews
      if (rawValue !== null && rawValue !== undefined && sig.key !== 'tests' && sig.key !== 'ci') {
        label.innerHTML = sig.label + '<br><span class="signal-bar-raw">' + rawValue + '</span>';
      } else {
        label.textContent = sig.label;
      }
      col.appendChild(track);
      col.appendChild(label);
      bars.appendChild(col);
    });

    card.appendChild(head);
    card.appendChild(monthlyWrap);
    card.appendChild(bars);
    card.addEventListener('click', function () { toggleDeveloperFilter(vm.handle); });
    return card;
  }

  function renderReviews(data) {
    var list = document.getElementById('reviewList');
    if (!list) return;
    list.innerHTML = '';
    buildReviewShareRows(data).forEach(function (row) {
      var el = document.createElement('div');
      el.className = 'review-row';
      el.setAttribute('data-handle', row.handle);
      var name = document.createElement('div');
      name.className = 'review-name';
      name.textContent = row.name;
      var track = document.createElement('div');
      track.className = 'review-bar-track';
      var fill = document.createElement('div');
      fill.className = 'review-bar-fill';
      fill.style.width = row.pct + '%';
      fill.style.background = row.color;
      track.appendChild(fill);
      var pct = document.createElement('div');
      pct.className = 'review-pct mono';
      pct.textContent = row.pct + '%';
      el.appendChild(name);
      el.appendChild(track);
      el.appendChild(pct);
      el.addEventListener('click', function () { toggleDeveloperFilter(row.handle); });
      list.appendChild(el);
    });
    var stats = computeReviewAggregateStats(data);
    var turnaroundText = stats.avgTurnaroundHours === null ? '—' : stats.avgTurnaroundHours.toFixed(1) + 'h';
    var crText = stats.changeRequestRate === null ? '—' : Math.round(stats.changeRequestRate * 100) + '%';
    var aggEl = document.getElementById('reviewAggregate');
    if (aggEl) {
      aggEl.innerHTML = 'Avg turnaround <b class="mono" style="color:#fff">' + turnaroundText +
        '</b> · Change-request rate <b class="mono" style="color:#fff">' + crText + '</b>';
    }
  }

  function renderDora(data) {
    var grid = document.getElementById('doraGrid');
    if (!grid) return;
    grid.innerHTML = '';
    buildDoraBoxes(data).forEach(function (box) {
      var el = document.createElement('div');
      el.className = 'dora-box';
      el.innerHTML = '<div class="dora-label">' + box.label + '</div>' +
        '<div class="dora-value mono" style="color:' + box.color + '">' + box.value + '</div>' +
        '<div class="dora-sub">' + box.sub + '</div>';
      grid.appendChild(el);
    });
  }

  // ---- Per-Repo Breakdown / H1 vs H2: DOM wiring -----------------------------
  // Reuses the same 'dashboard:developer-filter' CustomEvent contract
  // TASK-004-012/013 already dispatch on scorecard/review-row click — this
  // module only listens (REPOFILTER-INT-003: both panels update together, no
  // stale panel). See handoff "GAP-1 resolution" for why there's no separate
  // dedicated filter/clear control: TASK-004-012/013's click-to-toggle on a
  // scorecard or review row already dispatches null on a second click of the
  // same person, which is this dashboard's existing "clear filter" affordance.

  var currentMetricsData = null;

  function renderRepoTable(data) {
    var tbody = document.getElementById('repoTableBody');
    if (!tbody) return;
    tbody.innerHTML = '';
    buildRepoTableRows(data).forEach(function (row) {
      var tr = document.createElement('tr');
      tr.className = 'repo-row' + (row.muted ? ' muted' : '');
      tr.setAttribute('data-repo', row.repo);
      ['repo', 'commits', 'prs', 'reviews', 'ciPass', 'activeDevs'].forEach(function (field) {
        var td = document.createElement('td');
        td.className = 'mono';
        td.textContent = row[field];
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
  }

  function applyRepoFilterHighlight(data, handle) {
    var activeRepos = findDeveloperActiveRepos(data, handle);
    var rows = document.querySelectorAll('.repo-row');
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var isActive = !!activeRepos && activeRepos.indexOf(row.getAttribute('data-repo')) !== -1;
      row.classList.toggle('selected', !!handle && isActive);
      row.classList.toggle('dimmed', !!handle && !isActive);
    }
  }

  function renderH1H2(data, handle) {
    var dev = findDeveloperByHandle(data, handle);
    var h1 = dev ? dev.h1 : (data.team && data.team.h1);
    var h2 = dev ? dev.h2 : (data.team && data.team.h2);
    var teamH1 = dev && data.team ? data.team.h1 : null;
    var teamH2 = dev && data.team ? data.team.h2 : null;
    var grid = document.getElementById('h1h2Grid');
    if (grid) {
      grid.innerHTML = '';
      buildH1H2Cards(h1, h2, teamH1, teamH2).forEach(function (card) {
        var el = document.createElement('div');
        el.className = 'card';
        el.innerHTML = '<div class="h1h2-lab">' + card.label + '</div>' +
          '<div class="h1h2-val mono">' + card.text + '</div>' +
          '<div class="h1h2-delta ' + (card.delta.steady ? 'steady' : (card.delta.good ? 'up' : 'down')) + '">' +
          card.delta.text + '</div>';
        grid.appendChild(el);
      });
    }
    var scope = document.getElementById('h1h2Scope');
    if (scope) {
      scope.textContent = dev ? ('Filtered: ' + dev.name) : 'Team totals';
      scope.classList.toggle('filtered', !!dev);
    }
  }

  window.addEventListener('dashboard:developer-filter', function (evt) {
    if (!currentMetricsData) return;
    var handle = evt && evt.detail ? evt.detail.handle : null;
    applyRepoFilterHighlight(currentMetricsData, handle);
    renderH1H2(currentMetricsData, handle);
  });

  function init() {
    var data = readMetricsData();
    currentMetricsData = data;
    renderHeader(data);
    renderKpiRow(data);
    initActivityChart(data);
    renderScorecards(data);
    renderReviews(data);
    renderDora(data);
    renderRepoTable(data);
    renderH1H2(data, null);
    initScrollSpy();
    initSidebarCollapse();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Exposed for a future test harness (Node unit tests or Playwright page.evaluate)
  // without polluting the global scope on the deployed page.
  var exported = {
    resolveActiveSection: resolveActiveSection,
    computeCollapseState: computeCollapseState,
    formatStat: formatStat,
    monthLabel: monthLabel,
    formatWindowLabel: formatWindowLabel,
    formatCollected: formatCollected,
    CHART_PALETTE_9: CHART_PALETTE_9,
    formatCount: formatCount,
    sumMonthlyField: sumMonthlyField,
    avgSkipNull: avgSkipNull,
    monthShortLabel: monthShortLabel,
    computeH1H2DividerIndex: computeH1H2DividerIndex,
    computeDelta: computeDelta,
    buildKpiTiles: buildKpiTiles,
    buildTeamActivityChartConfig: buildTeamActivityChartConfig,
    buildPerDeveloperActivityChartConfig: buildPerDeveloperActivityChartConfig,
    applyDeveloperHighlight: applyDeveloperHighlight,
    hexToRgba: hexToRgba,
    buildScorecardFormulaLabel: buildScorecardFormulaLabel,
    buildScorecardViewModels: buildScorecardViewModels,
    SIGNAL_ORDER: SIGNAL_ORDER,
    formatSignalBarHeight: formatSignalBarHeight,
    formatSignalValue: formatSignalValue,
    buildSparklinePoints: buildSparklinePoints,
    REVIEW_BAR_COLORS: REVIEW_BAR_COLORS,
    buildReviewShareRows: buildReviewShareRows,
    computeReviewAggregateStats: computeReviewAggregateStats,
    buildDoraBoxes: buildDoraBoxes,
    formatCiPassRate: formatCiPassRate,
    repoActivityScore: repoActivityScore,
    sortRepoBreakdown: sortRepoBreakdown,
    isLowActivityRepo: isLowActivityRepo,
    buildRepoTableRows: buildRepoTableRows,
    findDeveloperByHandle: findDeveloperByHandle,
    findDeveloperActiveRepos: findDeveloperActiveRepos,
    H1H2_DEFS: H1H2_DEFS,
    formatH1H2Value: formatH1H2Value,
    buildH1H2Cards: buildH1H2Cards
  };
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = exported;
  } else {
    window.DashboardShell = exported;
  }
})();
