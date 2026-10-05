import { useState, useEffect, useRef } from "react";
import Sidebar from "./components/Sidebar";
import ChatInterface from "./components/ChatInterface";

const API = "/api";

export default function App() {
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(false);
  const [schema, setSchema] = useState(null);
  const [showSchema, setShowSchema] = useState(false);
  const [health, setHealth] = useState(null);
  const [agenticMode, setAgenticMode] = useState(false);
  const [conversationId, setConversationId] = useState(null);
  const [apiKey, setApiKey] = useState("");
  const [apiKeyDraft, setApiKeyDraft] = useState("");
  const [pendingClarification, setPendingClarification] = useState(null);
  const headers = { "Content-Type": "application/json", ...(apiKey ? { "X-API-Key": apiKey } : {}) };
  const inFlightRef = useRef(false);
  const requestIdRef = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    setSchema(null);
    fetch(`${API}/health`, { signal: controller.signal })
      .then((r) => r.json())
      .then(setHealth)
      .catch(() => {});
    fetch(`${API}/schema`, { headers: apiKey ? { "X-API-Key": apiKey } : {}, signal: controller.signal })
      .then((r) => r.json())
      .then((d) => { if (!controller.signal.aborted) setSchema(d.schema_text); })
      .catch(() => {});
    return () => controller.abort();
  }, [apiKey]);

  const sendQuery = async (query, clarification = null) => {
    const normalizedQuery = query.trim();
    if (!normalizedQuery || inFlightRef.current) return;

    inFlightRef.current = true;
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    const userMsg = { role: "user", content: normalizedQuery };
    setMessages((prev) => [...prev, userMsg]);
    setLoading(true);

    try {
      const res = await fetch(`${API}/query`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          query: normalizedQuery,
          conversation_id: conversationId,
          force_agentic: agenticMode,
          ...(clarification ? { clarification_id: clarification.id, clarification_answers: { meaning: clarification.answer } } : {}),
        }),
      });
      const data = await res.json();
      if (requestId !== requestIdRef.current) return;
      if (!res.ok) throw new Error(data.detail || "Request failed safely.");
      setConversationId(data.conversation_id);
      setPendingClarification(data.status === "needs_clarification" ? { ...data.clarification, query: normalizedQuery } : null);
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          ...data,
          content: data.response,
          sql: data.sql,
          data: data.data,
          visualization: data.visualization,
          success: data.success,
          trace_id: data.trace_id,
          metrics: data.metrics,
          agent_mode: data.agent_mode,
          agent_steps: data.agent_steps,
        },
      ]);
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: err.message || "Failed to connect to the server.",
          success: false,
        },
      ]);
    } finally {
      if (requestId === requestIdRef.current) {
        inFlightRef.current = false;
        setLoading(false);
      }
    }
  };

  const sendFeedback = async (traceId, value, comment) => {
    try {
      await fetch(`${API}/feedback`, {
        method: "POST",
        headers,
        body: JSON.stringify({ trace_id: traceId, value, comment }),
      });
    } catch {
      // silent fail
    }
  };

  const clearChat = () => {
    requestIdRef.current += 1;
    inFlightRef.current = false;
    setLoading(false);
    setMessages([]);
    setPendingClarification(null);
    setConversationId(null);
    fetch(`${API}/conversations/${conversationId}`, { method: "DELETE", headers }).catch(
      () => {}
    );
  };

  return (
    <div className="app-shell flex min-h-screen">
      <div className="flex flex-col flex-1 min-w-0">
        <form className="flex flex-wrap gap-3 px-5 py-3 border-b border-[var(--text-3)]" onSubmit={(event) => { event.preventDefault(); setApiKey(apiKeyDraft); setMessages([]); setConversationId(null); setPendingClarification(null); }}>
          <label htmlFor="api-key" className="text-sm text-[var(--text-2)]">Access key</label>
          <input id="api-key" type="password" autoComplete="off" value={apiKeyDraft} onChange={(event) => setApiKeyDraft(event.target.value)} className="bg-transparent border rounded px-2 text-[var(--text-1)]" />
          <button disabled={loading} className="text-sm text-[var(--accent)]" type="submit">Connect</button>
        </form>
        <div className="flex flex-1 min-w-0">
      <Sidebar
        health={health}
        schema={schema}
        showSchema={showSchema}
        setShowSchema={setShowSchema}
        loading={loading}
        onClear={clearChat}
        onSampleQuery={sendQuery}
      />
      <ChatInterface
        messages={messages}
        loading={loading}
        onSend={sendQuery}
        onFeedback={sendFeedback}
        agenticMode={agenticMode}
        onToggleAgentic={() => setAgenticMode((p) => !p)}
        pendingClarification={pendingClarification}
        onClarify={(answer) => sendQuery(pendingClarification.query, { id: pendingClarification.id, answer })}
        requestHeaders={headers}
      />
        </div>
      </div>
    </div>
  );
}
