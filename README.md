# 代購選品監控工具

靜態前端可部署到 GitHub Pages。

## 功能

- BEYBLADE X / 戰鬥陀螺 X 商品監控清單
- 日本、馬來西亞、香港、新加坡、泰國、韓國、台灣來源
- 一鍵查價表格
- 台幣成本與毛利試算
- LINE 訂單文字解析
- 本機 Ollama 分析

## 本地查價後端

GitHub Pages 只能執行前端，查價需在本機啟動後端：

```bat
start_daigou_tool.bat
```

或：

```bash
python daigou_price_backend.py
```

後端預設網址：

```text
http://127.0.0.1:8787
```
