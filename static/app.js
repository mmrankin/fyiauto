// fyiAuto front-end: dropdown auto-apply + AI search.

(function () {
  var filterForm = document.getElementById('filterForm');

  // Dropdowns / range inputs apply on change by reloading with new query args.
  if (filterForm) {
    filterForm.querySelectorAll('[data-filter]').forEach(function (el) {
      var evt = el.tagName === 'SELECT' ? 'change' : 'change';
      el.addEventListener(evt, function () {
        // Reset to page 1 whenever filters change.
        var p = filterForm.querySelector('[name=page]');
        if (p) p.value = 1;
        filterForm.submit();
      });
    });
  }

  // ---- AI search ----
  var aiBar = document.getElementById('aiBar');
  if (!aiBar) return;

  function money(n) {
    n = parseInt(n, 10);
    if (!n || n <= 0) return 'Call for price';
    return '$' + n.toLocaleString();
  }
  function miles(n) {
    n = parseInt(n, 10);
    return n > 0 ? n.toLocaleString() + ' mi' : '—';
  }
  function esc(s) {
    return (s == null ? '' : String(s)).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function card(v) {
    var photo = v.primary_photo
      ? '<img src="' + esc(v.primary_photo) + '" loading="lazy">'
      : '<div class="nophoto">📷 No photo</div>';
    var specs = ['drivetrain', 'fuel_type', 'ext_color']
      .filter(function (k) { return v[k]; })
      .map(function (k) { return '<span>' + esc(v[k]) + '</span>'; }).join('');
    return '<a class="card" href="/vehicle/' + esc(v.vin) + '">' +
      '<div class="card-photo">' + photo +
        (v.condition ? '<span class="badge badge-' + esc(v.condition) + '">' + esc(v.condition) + '</span>' : '') +
        (v.photo_count ? '<span class="pcount">' + v.photo_count + ' 📷</span>' : '') +
      '</div><div class="card-body">' +
      '<div class="card-title">' + esc((v.year || '') + ' ' + (v.make || 'Vehicle') + ' ' + (v.model || '')) + '</div>' +
      '<div class="card-trim">' + esc(v.trim || v.body || '') + '</div>' +
      '<div class="card-meta"><span class="price">' + money(v.price) + '</span>' +
      '<span class="miles">' + miles(v.mileage) + '</span></div>' +
      '<div class="card-specs">' + specs + '</div></div></a>';
  }

  function note(interpreted) {
    var el = document.getElementById('aiNote');
    if (!el) return;
    var f = interpreted.filters || {};
    var tags = Object.keys(f).filter(function (k) { return k !== 'q'; })
      .map(function (k) { return '<span class="tag">' + esc(k.replace('_', ' ')) + ': ' + esc(f[k]) + '</span>'; });
    if (f.q) tags.push('<span class="tag">keywords: ' + esc(f.q) + '</span>');
    el.innerHTML = '<b>Interpreted your search</b> ' +
      (tags.length ? tags.join(' ') : '<span class="tag">no specific filters — showing best matches</span>');
    el.classList.add('show');
  }

  aiBar.addEventListener('submit', function (e) {
    e.preventDefault();
    var q = document.getElementById('aiInput').value.trim();
    if (!q) return;
    var grid = document.getElementById('grid');
    var count = document.getElementById('resultCount');
    if (grid) grid.style.opacity = '.4';

    fetch('/api/ai-search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ q: q })
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.error) throw new Error(data.error);
        if (data.redirect) { window.location = data.redirect; return; }
        note(data.interpreted || {});
        var rows = (data.results && data.results.results) || [];
        if (grid) {
          grid.style.opacity = '1';
          grid.innerHTML = rows.length
            ? rows.map(card).join('')
            : '<p class="empty">No matches. Try different wording or the filters on the left.</p>';
        }
        if (count) count.textContent = (data.results && data.results.total || 0).toLocaleString();
      })
      .catch(function (err) {
        if (grid) grid.style.opacity = '1';
        // Fall back to a plain keyword search via normal navigation.
        window.location = '/?q=' + encodeURIComponent(q);
      });
  });
})();
