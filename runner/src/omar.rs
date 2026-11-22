//! MCP stdio client for the OMAR server: spawns `omar mcp-server` and trades
//! line-delimited JSON-RPC. Config from OMAR_DIR, OMAR_EA_ID, OMAR_TMUX_SERVER.

use anyhow::{anyhow, Context, Result};
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

const MCP_PROTOCOL_VERSION: &str = "2024-11-05";

/// Parent every MASS agent is spawned under: the OMAR engineering agent.
const PARENT_AGENT: &str = "ea";

pub struct OmarClient {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    next_id: u64,
}

/// Launch command for the agy backend (skips permission prompts).
const AGY_BASE_COMMAND: &str = "agy --dangerously-skip-permissions";

/// True when a parsed JSON-RPC line is the response to `id` (a notification
/// has no id; another request's reply has a different one).
fn is_response_for(value: &Value, id: u64) -> bool {
    value.get("id").and_then(Value::as_u64) == Some(id)
}

/// Single-quote a value so spaces survive the shell agy runs under.
fn shell_single_quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\\''"))
}

/// Build the agy launch command, quoting the model label. No model means agy's default.
fn agy_spawn_command(model: Option<&str>) -> String {
    match model {
        Some(model) => format!("{AGY_BASE_COMMAND} --model {}", shell_single_quote(model)),
        None => AGY_BASE_COMMAND.to_string(),
    }
}

/// Find the omar binary: $OMAR_BIN, then a sibling of this exe, then PATH.
pub fn resolve_omar_binary() -> PathBuf {
    if let Some(path) = std::env::var_os("OMAR_BIN") {
        return PathBuf::from(path);
    }
    if let Ok(exe) = std::env::current_exe() {
        let sibling = exe.with_file_name("omar");
        if sibling.is_file() {
            return sibling;
        }
    }
    PathBuf::from("omar")
}

impl OmarClient {
    pub fn start() -> Result<Self> {
        let binary = resolve_omar_binary();
        let mut child = Command::new(&binary)
            .arg("mcp-server")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            // Surface server-side config errors in our stderr.
            .stderr(Stdio::inherit())
            .spawn()
            .with_context(|| format!("Failed to spawn {:?} mcp-server", binary))?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| anyhow!("mcp-server child has no stdin"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| anyhow!("mcp-server child has no stdout"))?;
        let mut client = OmarClient {
            child,
            stdin,
            stdout: BufReader::new(stdout),
            next_id: 1,
        };
        client.request(
            "initialize",
            json!({
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "omar-mass", "version": env!("CARGO_PKG_VERSION")},
            }),
        )?;
        Ok(client)
    }

    fn request(&mut self, method: &str, params: Value) -> Result<Value> {
        let id = self.next_id;
        self.next_id += 1;
        let req = json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params});
        let mut line = serde_json::to_vec(&req)?;
        line.push(b'\n');
        self.stdin
            .write_all(&line)
            .context("Failed to write MCP request")?;
        self.stdin.flush().context("Failed to flush MCP stdin")?;

        // Read until our response id arrives, skipping server notifications and
        // any non-JSON log lines (a single read_line would desync on those).
        loop {
            let mut buf = String::new();
            let n = self
                .stdout
                .read_line(&mut buf)
                .context("Failed to read MCP response")?;
            if n == 0 {
                return Err(anyhow!("MCP server closed stdout unexpectedly"));
            }
            let line = buf.trim();
            let resp: Value = match serde_json::from_str(line) {
                Ok(v) => v,
                Err(_) => continue, // not JSON-RPC (log/progress line); skip
            };
            if !is_response_for(&resp, id) {
                continue; // notification or another request's reply; skip
            }
            if let Some(err) = resp.get("error") {
                return Err(anyhow!(
                    "MCP server error: {}",
                    err.get("message")
                        .and_then(Value::as_str)
                        .unwrap_or("unknown")
                ));
            }
            return resp
                .get("result")
                .cloned()
                .ok_or_else(|| anyhow!("MCP response missing 'result'"));
        }
    }

    /// Call a tool; returns `structuredContent` on success.
    pub fn call_tool(&mut self, name: &str, arguments: Value) -> Result<Value> {
        let result = self.request("tools/call", json!({"name": name, "arguments": arguments}))?;
        if result.get("isError").and_then(Value::as_bool) == Some(true) {
            let msg = result
                .get("content")
                .and_then(Value::as_array)
                .and_then(|a| a.first())
                .and_then(|v| v.get("text"))
                .and_then(Value::as_str)
                .unwrap_or("unknown tool error");
            return Err(anyhow!("tool {} failed: {}", name, msg));
        }
        Ok(result
            .get("structuredContent")
            .cloned()
            .unwrap_or(Value::Null))
    }

    // ---- typed helpers over the OMAR tool surface ----

    pub fn add_project(&mut self, name: &str) -> Result<usize> {
        let v = self.call_tool("add_project", json!({"name": name}))?;
        v.get("project_id")
            .and_then(Value::as_u64)
            .map(|id| id as usize)
            .ok_or_else(|| anyhow!("add_project returned no project_id: {}", v))
    }

    pub fn list_projects(&mut self) -> Result<Vec<(usize, String)>> {
        let v = self.call_tool("list_projects", json!({}))?;
        Ok(v.get("projects")
            .and_then(Value::as_array)
            .map(|projects| {
                projects
                    .iter()
                    .filter_map(|p| {
                        Some((
                            p.get("id").and_then(Value::as_u64)? as usize,
                            p.get("name").and_then(Value::as_str)?.to_string(),
                        ))
                    })
                    .collect()
            })
            .unwrap_or_default())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn spawn_agent(
        &mut self,
        name: &str,
        project_id: usize,
        task: &str,
        backend: &str,
        model: Option<&str>,
        workdir: &str,
    ) -> Result<()> {
        let mut args = json!({
            "name": name,
            "project_id": project_id,
            "task": task,
            "workdir": workdir,
            "parent": PARENT_AGENT,
        });
        // agy model labels have spaces that OMAR's --model path rejects; route
        // agy through the raw command (OMAR infers backend=agy from token 1).
        if backend == "agy" {
            args["command"] = json!(agy_spawn_command(model));
        } else {
            args["backend"] = json!(backend);
            if let Some(model) = model {
                args["model"] = json!(model);
            }
        }
        self.call_tool("spawn_agent", args)?;
        Ok(())
    }

    /// Short names of all running agents in the EA.
    pub fn list_agents(&mut self) -> Result<Vec<String>> {
        let v = self.call_tool("list_agents", json!({}))?;
        Ok(v.get("agents")
            .and_then(Value::as_array)
            .map(|agents| {
                agents
                    .iter()
                    .filter_map(|a| {
                        a.get("id")
                            .or_else(|| a.get("name"))
                            .and_then(Value::as_str)
                            .map(str::to_string)
                    })
                    .collect()
            })
            .unwrap_or_default())
    }

    /// Send a line to an agent and wait for it to land.
    /// Verified delivery, so a dropped keystroke can't stall a whole wave.
    pub fn send_input(&mut self, name: &str, text: &str) -> Result<()> {
        self.call_tool(
            "send_input",
            json!({"name": name, "text": text, "enter": true, "verified": true}),
        )?;
        Ok(())
    }

    pub fn kill_agent(&mut self, name: &str) -> Result<()> {
        self.call_tool("kill_agent", json!({"name": name}))?;
        Ok(())
    }

    pub fn complete_project(&mut self, project_id: usize) -> Result<()> {
        self.call_tool("complete_project", json!({"project_id": project_id}))?;
        Ok(())
    }

    pub fn log_justification(
        &mut self,
        agent: &str,
        action: &str,
        justification: &str,
    ) -> Result<()> {
        self.call_tool(
            "log_justification",
            json!({"agent_name": agent, "action": action, "justification": justification}),
        )?;
        Ok(())
    }
}

impl Drop for OmarClient {
    fn drop(&mut self) {
        // Kill so a panicking runner can't leak mcp-server children.
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn agy_command_quotes_labelled_model() {
        // A label with spaces stays one --model argument.
        let cmd = agy_spawn_command(Some("Some Model (Low)"));
        assert_eq!(
            cmd,
            "agy --dangerously-skip-permissions --model 'Some Model (Low)'"
        );
        assert!(cmd.starts_with("agy "), "first token must stay 'agy'");
    }

    #[test]
    fn agy_command_without_model_uses_default() {
        assert_eq!(
            agy_spawn_command(None),
            "agy --dangerously-skip-permissions"
        );
    }

    #[test]
    fn shell_single_quote_escapes_embedded_quote() {
        assert_eq!(shell_single_quote("a'b"), "'a'\\''b'");
    }

    #[test]
    fn is_response_for_matches_only_same_id() {
        use serde_json::json;
        assert!(is_response_for(&json!({"id": 3, "result": {}}), 3));
        // Different id (another reply) and notifications (no id) are skipped.
        assert!(!is_response_for(&json!({"id": 2, "result": {}}), 3));
        assert!(!is_response_for(&json!({"method": "progress"}), 3));
    }
}
