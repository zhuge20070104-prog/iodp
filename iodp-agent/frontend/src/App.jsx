import React, { useState, useRef, useEffect } from 'react'

// API endpoint: 部署后替换为 Terraform output 的 api_endpoint
const API_BASE = window.__IODP_API_BASE__ || ''

function App() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [threadId, setThreadId] = useState(null)
  const messagesEndRef = useRef(null)

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const pollResult = async (jobId) => {
    const maxAttempts = 60
    for (let i = 0; i < maxAttempts; i++) {
      await new Promise(r => setTimeout(r, 2000))
      const res = await fetch(`${API_BASE}/diagnose/${jobId}`)
      const data = await res.json()

      if (data.status === 'completed') {
        return data.result
      }
      if (data.status === 'failed') {
        throw new Error(data.error || '诊断失败')
      }
    }
    throw new Error('诊断超时，请稍后重试')
  }

  const sendMessage = async () => {
    if (!input.trim() || loading) return

    const userMsg = input.trim()
    setInput('')
    setMessages(prev => [...prev, { role: 'user', content: userMsg }])
    setLoading(true)

    try {
      const res = await fetch(`${API_BASE}/diagnose`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: userMsg,
          thread_id: threadId,
        }),
      })

      if (!res.ok) throw new Error(`HTTP ${res.status}`)

      const job = await res.json()
      setThreadId(job.thread_id)

      setMessages(prev => [...prev, { role: 'system', content: '正在分析中...' }])

      const result = await pollResult(job.job_id)

      setMessages(prev => {
        const filtered = prev.filter(m => m.content !== '正在分析中...')
        const msgs = [...filtered]
        if (result.user_reply) {
          msgs.push({ role: 'assistant', content: result.user_reply })
        }
        if (result.bug_report) {
          msgs.push({
            role: 'report',
            content: JSON.stringify(result.bug_report, null, 2),
          })
        }
        return msgs
      })
    } catch (err) {
      setMessages(prev => {
        const filtered = prev.filter(m => m.content !== '正在分析中...')
        return [...filtered, { role: 'error', content: err.message }]
      })
    } finally {
      setLoading(false)
    }
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage()
    }
  }

  return (
    <div style={styles.container}>
      <header style={styles.header}>
        <h1 style={styles.title}>IODP 智能故障诊断</h1>
        <span style={styles.badge}>Agent v2.0</span>
      </header>

      <div style={styles.chatArea}>
        {messages.length === 0 && (
          <div style={styles.placeholder}>
            <p>描述您遇到的问题，例如：</p>
            <p style={styles.example}>"我昨晚11点支付一直失败，页面卡在加载中"</p>
          </div>
        )}
        {messages.map((msg, i) => (
          <div key={i} style={{
            ...styles.msgRow,
            justifyContent: msg.role === 'user' ? 'flex-end' : 'flex-start',
          }}>
            <div style={{
              ...styles.bubble,
              ...(msg.role === 'user' ? styles.userBubble :
                  msg.role === 'report' ? styles.reportBubble :
                  msg.role === 'error' ? styles.errorBubble :
                  msg.role === 'system' ? styles.systemBubble :
                  styles.assistantBubble),
            }}>
              {msg.role === 'report' && <div style={styles.reportLabel}>Bug Report</div>}
              {msg.role === 'report' ? (
                <pre style={styles.pre}>{msg.content}</pre>
              ) : (
                msg.content
              )}
            </div>
          </div>
        ))}
        <div ref={messagesEndRef} />
      </div>

      <div style={styles.inputArea}>
        <textarea
          style={styles.textarea}
          rows={1}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="描述您遇到的问题..."
          disabled={loading}
        />
        <button style={styles.sendBtn} onClick={sendMessage} disabled={loading}>
          {loading ? '...' : '发送'}
        </button>
      </div>
    </div>
  )
}

const styles = {
  container: {
    maxWidth: 720, margin: '0 auto', height: '100vh',
    display: 'flex', flexDirection: 'column',
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    background: '#f5f5f5',
  },
  header: {
    padding: '16px 20px', background: '#1a1a2e', color: '#fff',
    display: 'flex', alignItems: 'center', gap: 12,
  },
  title: { margin: 0, fontSize: 18, fontWeight: 600 },
  badge: {
    fontSize: 11, background: '#16213e', padding: '2px 8px',
    borderRadius: 4, color: '#7f8fa6',
  },
  chatArea: { flex: 1, overflow: 'auto', padding: 20 },
  placeholder: { textAlign: 'center', color: '#999', marginTop: 80 },
  example: { color: '#555', fontStyle: 'italic', marginTop: 8 },
  msgRow: { display: 'flex', marginBottom: 12 },
  bubble: {
    maxWidth: '75%', padding: '10px 14px', borderRadius: 12,
    fontSize: 14, lineHeight: 1.6, wordBreak: 'break-word',
  },
  userBubble: { background: '#0084ff', color: '#fff' },
  assistantBubble: { background: '#fff', color: '#333', border: '1px solid #e0e0e0' },
  reportBubble: {
    background: '#1a1a2e', color: '#a4b0be', border: '1px solid #2d3436',
    width: '100%', maxWidth: '100%',
  },
  errorBubble: { background: '#ffe0e0', color: '#c0392b' },
  systemBubble: { background: 'transparent', color: '#999', fontStyle: 'italic' },
  reportLabel: {
    fontSize: 11, color: '#7f8fa6', marginBottom: 6,
    textTransform: 'uppercase', letterSpacing: 1,
  },
  pre: { margin: 0, fontSize: 12, whiteSpace: 'pre-wrap', fontFamily: 'monospace' },
  inputArea: {
    padding: '12px 20px', background: '#fff', borderTop: '1px solid #e0e0e0',
    display: 'flex', gap: 8,
  },
  textarea: {
    flex: 1, border: '1px solid #ddd', borderRadius: 8, padding: '10px 12px',
    fontSize: 14, resize: 'none', outline: 'none', fontFamily: 'inherit',
  },
  sendBtn: {
    padding: '0 20px', background: '#0084ff', color: '#fff', border: 'none',
    borderRadius: 8, cursor: 'pointer', fontSize: 14, fontWeight: 600,
  },
}

export default App
