/** 开发期调试：把场景合成为一张 PNG 上传到 :8981 */
;(function () {
  window.__comp = async function (name) {
    const wrap = document.querySelector('.ws-chart-wrap')
    if (!wrap) return 'no-wrap'
    const wr = wrap.getBoundingClientRect()
    const S = 1920 / wr.width
    const W = 1920
    const H = Math.round(wr.height * S)
    const c = document.createElement('canvas')
    c.width = W
    c.height = H
    const ctx = c.getContext('2d')
    ctx.fillStyle = '#04141f'
    ctx.fillRect(0, 0, W, H)
    const px = (el) => {
      const r = el.getBoundingClientRect()
      return [(r.left - wr.left) * S, (r.top - wr.top) * S, r.width * S, r.height * S]
    }
    for (const sel of ['.ws-harbor-bg', '.ws-harbor-vector', '.ws-npp-layer', '.ws-bg-ship-layer', '.ws-ship-layer']) {
      const svg = document.querySelector(sel)
      if (!svg) continue
      const xml = new XMLSerializer().serializeToString(svg)
      const url = URL.createObjectURL(new Blob([xml], { type: 'image/svg+xml;charset=utf-8' }))
      const img = new Image()
      await new Promise((r) => { img.onload = r; img.onerror = r; img.src = url })
      ctx.drawImage(img, 0, 0, W, H)
    }
    const ec = document.querySelector('.ws-chart canvas')
    if (ec) { const b = px(ec); ctx.drawImage(ec, b[0], b[1], b[2], b[3]) }
    for (const bb of document.querySelectorAll('.ws-npp-billboard, .ws-ship-billboard')) {
      const b = px(bb)
      const im = bb.querySelector('img')
      if (!im || !im.complete || !im.naturalWidth) continue
      ctx.drawImage(im, b[0], b[1], b[2], b[3])
    }
    for (const lb of document.querySelectorAll('.ws-bg-label, .ws-scene-upright-label')) {
      if (lb.classList.contains('is-hidden')) continue
      const b = px(lb)
      const cs = getComputedStyle(lb)
      ctx.font = (cs.fontWeight || 'normal') + ' ' + cs.fontSize + 'px ' + (cs.fontFamily || 'sans-serif')
      ctx.fillStyle = cs.color
      ctx.textBaseline = 'top'
      ctx.fillText(lb.textContent.trim(), b[0], b[1])
    }
    const d = c.toDataURL('image/png').split(',')[1]
    const r = await fetch('http://localhost:8981/' + name, { method: 'POST', body: d })
    return 'status:' + r.status
  }
  return 'ready'
})()
