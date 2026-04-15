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

