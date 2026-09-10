/**
 * `<SessionSwitcher>` (R2.16) — pick a workspace, or start a new one.
 *
 * A session is a graph over a shared corpus. Switching costs nothing and
 * starting a new one costs nothing either: `papers`, `authors` and `edges` are
 * global, so a second session pays no API calls for anything the first already
 * fetched. That is the payoff BUILD.md asks to be demonstrated, and it is why
 * this control can be a plain dropdown rather than something that warns you.
 *
 * **The node count is in every option.** Without it the list is a row of names
 * where the session holding your work looks exactly like the empty one you
 * created by accident.
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";

export interface SessionSwitcherProps {
  sessionId: number;
  onSwitch: (id: number) => void;
}

export function SessionSwitcher({ sessionId, onSwitch }: SessionSwitcherProps) {
  const [naming, setNaming] = useState(false);
  const [name, setName] = useState("");
  const queryClient = useQueryClient();

  const sessions = useQuery({ queryKey: ["sessions"], queryFn: api.sessions });

  const create = useMutation({
    mutationFn: () => api.createSession(name.trim()),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: ["sessions"] });
      setNaming(false);
      setName("");
      // Switch straight into it. Creating a workspace and then having to find
      // it in a dropdown is a step nobody wants.
      onSwitch(created.id);
    },
  });

  if (naming) {
    return (
      <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <input
          value={name}
          onChange={(event) => setName(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && name.trim()) create.mutate();
            if (event.key === "Escape") setNaming(false);
          }}
          placeholder="New session name"
          aria-label="New session name"
          autoFocus
          style={{ width: 150 }}
        />
        <button type="button" onClick={() => create.mutate()} disabled={!name.trim() || create.isPending}>
          {create.isPending ? "…" : "Create"}
        </button>
        <button type="button" onClick={() => setNaming(false)}>
          Cancel
        </button>
      </span>
    );
  }

  return (
    <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <select
        aria-label="Session"
        value={sessionId}
        onChange={(event) => onSwitch(Number(event.target.value))}
        disabled={!sessions.data}
      >
        {(sessions.data ?? []).map((session) => (
          <option key={session.id} value={session.id}>
            {session.name} ({session.node_count})
          </option>
        ))}
      </select>
      <button type="button" onClick={() => setNaming(true)} title="Start a new graph over the same papers">
        + Session
      </button>
    </span>
  );
}
