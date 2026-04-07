// Background script - 仅负责代理 API 请求以绕过浏览器 CORS
console.log('[Background] API Proxy 背景脚本已启动');

// 监听来自content script的消息
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  // 处理 API 请求（绕过 CORS）
  if (request.action === 'apiRequest') {
    // console.log('[Background] 收到 API 请求:', request.url);
    
    fetch(request.url, {
      method: request.method || 'POST',
      headers: request.headers || {
        'Content-Type': 'application/json'
      },
      body: request.body ? JSON.stringify(request.body) : undefined
    })
    .then(response => {
      if (!response.ok) {
        return response.text().then(text => {
          throw new Error(`API请求失败: ${response.status} - ${text}`);
        });
      }
      return response.json();
    })
    .then(data => {
      sendResponse({ success: true, data: data });
    })
    .catch(error => {
      console.error('[Background] API 请求失败:', error);
      sendResponse({ success: false, error: error.message });
    });

    return true; // 保持消息通道开放
  }
});

// SSE 流式：content 通过 long-lived Port 接收增量事件（data: {...}\\n\\n）
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== 'chatStream') {
    return;
  }
  port.onMessage.addListener(async (msg) => {
    if (msg.action !== 'start' || !msg.url) {
      return;
    }
    try {
      const res = await fetch(msg.url, {
        method: msg.method || 'POST',
        headers: msg.headers || { 'Content-Type': 'application/json' },
        body: msg.body !== undefined ? JSON.stringify(msg.body) : undefined,
      });
      if (!res.ok) {
        const text = await res.text();
        port.postMessage({ ok: false, error: `HTTP ${res.status}: ${text}` });
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        const chunks = buffer.split('\n\n');
        buffer = chunks.pop() || '';
        for (const block of chunks) {
          const line = block.trim();
          if (!line.startsWith('data:')) {
            continue;
          }
          const jsonStr = line.replace(/^data:\s*/, '');
          try {
            const payload = JSON.parse(jsonStr);
            port.postMessage({ ok: true, event: payload });
          } catch (e) {
            console.warn('[Background] SSE 解析跳过:', jsonStr.slice(0, 80));
          }
        }
      }
      port.postMessage({ ok: true, finished: true });
    } catch (error) {
      console.error('[Background] 流式请求失败:', error);
      port.postMessage({ ok: false, error: error.message || String(error) });
    }
  });
});


