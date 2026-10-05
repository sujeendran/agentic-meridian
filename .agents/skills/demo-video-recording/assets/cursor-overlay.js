// Visible cursor and click ripple for Playwright recordings, which show no mouse pointer.
// Install before the visible part of a take:
//   await page.addInitScript({ path: '<skill-dir>/assets/cursor-overlay.js' });
//   await page.reload();
// The dot follows Playwright's synthetic mouse moves, so move with `steps` to animate it.
(() => {
  const install = () => {
    if (document.getElementById('demo-cursor')) return;
    const style = document.createElement('style');
    style.textContent = `
      #demo-cursor { position: fixed; z-index: 2147483647; pointer-events: none; width: 18px; height: 18px;
        margin: -9px 0 0 -9px; border-radius: 50%; background: rgba(42,120,214,.35);
        border: 2px solid #2a78d6; left: 50vw; top: 50vh; }
      .demo-ripple { position: fixed; z-index: 2147483646; pointer-events: none; width: 16px; height: 16px;
        margin: -8px 0 0 -8px; border-radius: 50%; border: 3px solid #2a78d6;
        animation: demo-ripple .6s ease-out forwards; }
      @keyframes demo-ripple { to { transform: scale(3.2); opacity: 0; } }`;
    document.head.appendChild(style);
    const cursor = document.createElement('div');
    cursor.id = 'demo-cursor';
    document.body.appendChild(cursor);
    addEventListener('mousemove', (e) => {
      cursor.style.left = e.clientX + 'px';
      cursor.style.top = e.clientY + 'px';
    }, true);
    addEventListener('mousedown', (e) => {
      const ripple = document.createElement('div');
      ripple.className = 'demo-ripple';
      ripple.style.left = e.clientX + 'px';
      ripple.style.top = e.clientY + 'px';
      document.body.appendChild(ripple);
      setTimeout(() => ripple.remove(), 700);
    }, true);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install);
  else install();
})();
