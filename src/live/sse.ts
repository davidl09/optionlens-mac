// Server-Sent Events framing (text/event-stream): chunks in, complete events out. Pure.

export interface SseEvent { event: string; data: string }

export class SseParser {
  private buf = "";

  /** Feed a decoded chunk; returns the events it completed (comments and keep-alives are dropped). */
  feed(chunk: string): SseEvent[] {
    this.buf += chunk.replace(/\r\n?/g, "\n");
    const out: SseEvent[] = [];
    let end: number;
    while ((end = this.buf.indexOf("\n\n")) >= 0) {
      const block = this.buf.slice(0, end);
      this.buf = this.buf.slice(end + 2);
      let event = "message";
      const data: string[] = [];
      for (const line of block.split("\n")) {
        if (!line || line.startsWith(":")) continue;
        const i = line.indexOf(":");
        const field = i < 0 ? line : line.slice(0, i);
        const value = i < 0 ? "" : line.slice(i + 1).replace(/^ /, "");
        if (field === "event") event = value;
        else if (field === "data") data.push(value);
      }
      if (data.length) out.push({ event, data: data.join("\n") });
    }
    return out;
  }
}
