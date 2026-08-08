"""测量 reasonix 滚动时的 JS 长任务（卡顿的直接证据）+ 输出速率。"""
import asyncio
import time

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:4790/"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = await browser.new_context(viewport={"width": 390, "height": 844},
                                        has_touch=True, is_mobile=True)
        await ctx.route("**/*", lambda route: route.continue_(
            headers={**route.request.headers, "Cache-Control": "no-cache"}))
        page = await ctx.new_page()
        await page.goto(BASE, wait_until="domcontentloaded", timeout=12000)
        await page.wait_for_timeout(2500)

        # 激活 reasonix 会话（找最后一个 running 的 reasonix 标签）
        activated = await page.evaluate("""() => {
            const tabs = document.querySelectorAll('#tabs .tab');
            // 尝试找 reasonix 工具标签
            for (const t of tabs) {
                if (t.dataset.tool === 'reasonix') { t.click(); return 'reasonix-tab'; }
            }
            if (tabs.length) { tabs[tabs.length - 1].click(); return 'last'; }
            return false;
        }""")
        print("激活:", activated)
        await page.wait_for_timeout(3000)

        # 注入长任务监听
        await page.evaluate("""() => {
            window.__longTasks = [];
            new PerformanceObserver((list) => {
                for (const e of list.getEntries()) {
                    window.__longTasks.push({ dur: Math.round(e.duration), start: Math.round(e.startTime) });
                }
            }).observe({ entryTypes: ['longtask'] });
            window.__wsBytes = 0;
            const origSend = WebSocket.prototype.send;
            // 统计 WS 收到的字节（通过 monkey-patch message 大小不可行，改用 xterm write 计数）
        }""")

        # 触摸滑动（reasonix 界面）
        await page.touchscreen.tap(195, 500)
        await page.mouse.move(195, 600)
        await page.mouse.down()
        for i in range(1, 13):
            await page.mouse.move(195, 600 - i * 30, steps=2)
            await page.wait_for_timeout(16)
        await page.mouse.up()
        await page.wait_for_timeout(1500)

        longs = await page.evaluate("window.__longTasks || []")
        print("滚动期间长任务数:", len(longs))
        if longs:
            print("  最长:", max(l["dur"] for l in longs), "ms; 明细:", longs[:8])
        else:
            print("  无长任务（滚动流畅）")

        await browser.close()


asyncio.run(main())
