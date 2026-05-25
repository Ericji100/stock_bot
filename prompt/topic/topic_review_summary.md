# 題材變更審核摘要提示詞
（可選使用，需明確傳入 --ai 參數才會呼叫，預設使用本地格式化）

變更包：
{change_pack_json}

摘要要求：
1. 繁體中文輸出
2. 每個 action 簡要說明（1-2 行）
3. 標注信心度與風險警告
4. 說明每個 action 的具體影響
5. 最後提供「下一步」操作提示

輸出格式：
```
【AI題材庫變更建議】

摘要：{summary}
信心度：{confidence}
變更模式：{mode}

📋 變更內容：

1. 【{action_type}】{theme_name}
   ID：{theme_id}
   原因：{reason}
   信心度：{confidence}
   證據：{evidence_summary}

⚠️ 警告：{warnings}

📌 下一步：
請選擇：
• /topic_confirm {change_id} - 確認套用
• /topic_reject {change_id} - 拒絕
• /topic_maintain - 重新產生或更新
```
