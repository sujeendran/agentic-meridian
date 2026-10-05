  // ---- step helpers (prepended to every step by scripts/make_step.py) ----
  // __T0__ is replaced with the recording's start time (epoch ms). Adjust the selectors
  // below to the app being recorded; everything else is app-independent.
  const T0 = __T0__;
  const INPUT_SELECTOR = '#input';        // the text box `say()` types into
  const BUSY_SELECTOR = '.typing';        // shown while the app/agent is still working ('' to skip)
  const LAST_REPLY_SELECTOR = '.msg.assistant'; // latest output whose text should stop changing ('' to skip)

  const marks = [];
  const mark = (name) => marks.push([name, +((Date.now() - T0) / 1000).toFixed(2)]);
  const pause = (ms) => page.waitForTimeout(ms);

  // Move the (overlay) cursor smoothly to an element, then click it.
  const moveTo = async (locator) => {
    const box = await locator.boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 30 });
    await pause(250);
  };
  const click = async (locator) => { await moveTo(locator); await locator.click(); await pause(400); };

  // Type like a person (visible keystrokes) and submit.
  const say = async (text, selector = INPUT_SELECTOR) => {
    const input = page.locator(selector);
    await click(input);
    await input.pressSequentially(text, { delay: 30 });
    await pause(500);
    await input.press('Enter');
    await pause(1500);
  };

  // Wait until the app is idle and the latest reply has stopped changing (streamed text).
  const settle = async (timeout = 150000) => {
    if (BUSY_SELECTOR) {
      await page.waitForFunction((sel) => !document.querySelector(sel), BUSY_SELECTOR, { timeout });
    }
    let last = '';
    for (let i = 0; i < 80 && LAST_REPLY_SELECTOR; i++) {
      await pause(1500);
      const text = await page.locator(LAST_REPLY_SELECTOR).last().innerText().catch(() => '');
      if (text && text === last) return text;
      last = text;
    }
    return last;
  };

  // Poll a page condition without throwing; keep each MCP call under ~2 minutes and repeat.
  const waitIdle = (predicate, arg, timeout = 115000) =>
    page.waitForFunction(predicate, arg, { timeout }).then(() => true).catch(() => false);

  // Smooth-scroll a scrollable container (or the window when selector is null).
  const scrollTo = async (selector, top, wait = 3500) => {
    if (selector) await page.locator(selector).evaluate((el, y) => el.scrollTo({ top: y, behavior: 'smooth' }), top);
    else await page.evaluate((y) => window.scrollTo({ top: y, behavior: 'smooth' }), top);
    await pause(wait);
  };
  // ---- end helpers ----
