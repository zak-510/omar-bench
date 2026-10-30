Design an OMAR team that will complete the task described below, and propose it with
`omar_propose_design`. The workflow is deployed as you propose it.

Nobody is watching this chat and nobody can answer a question. Where something is
unclear, decide, and say in your proposal what you assumed. Do not do the task
yourself and do not spawn agents: propose one complete program. You may read the
task's files to design the team, but do not try the solution, in place or in a copy:
that is the team's work.

The task. Its statement is in the file @STATEMENT@: read it in full before you design
anything. It is graded on this machine, by tests you cannot see, in the place the
statement names, with @CWD@ as the working directory for anything it leaves relative.
Nothing in an OMAR workspace is graded. So every agent's prompt must tell it to make
the task's changes in place, at those paths, and to use its workspace only for its
own notes.

Agents. Only @BACKEND@ is installed here: every agent is a `@BACKEND@` agent. Do not
add Jev steps: they need an API key this machine does not have. There is no Rust
toolchain either, so no code reactions (`{= ... =}` bodies): they are compiled with
cargo when the run starts. Every reaction is an agent's task.

The language. Your compiler is the one in this program's version, whose reference is
@SPEC@. Read that file rather than the copy online when you need more than your
summary.

Running it. Give every input the program declares a value in the proposal's
`inputs`; the run starts with those and nothing else. Each agent turn may take up to
@TURN_HOURS@ hours. The attempt ends when the program finishes, so design it to
finish once the task is done.

While it runs you will get check-ins with what the team has produced so far. Change
the workflow only when that work shows the design will not complete the task: a
change restarts every agent with a fresh conversation. A change is applied between
tags, and a plain connection (`a -> b`) delivers within the tag it was written in, so
stages joined by plain connections run as one tag that nothing can change until it
ends. Join each agent's stage to the next with `after 0` (`a -> b after 0`): it costs
no time, starts a new tag, and makes that stage's result visible to the check-ins.
