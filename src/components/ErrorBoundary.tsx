// Keeps a rendering bug in one part of the window from blanking the whole app: the part shows the error and a
// "Try again" button instead. A change of `resetKey` (the route, for the page area) also clears it. React logs the
// caught error to the console, which the main process writes to main.log.
import { Component, type ReactNode } from "react";

interface Props { children: ReactNode; label: string; resetKey?: string }
interface State { error: Error | null; key?: string }

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, key: this.props.resetKey };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    return props.resetKey !== state.key ? { error: null, key: props.resetKey } : null;
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="error-boundary" role="alert">
        <b>The {this.props.label} hit an error.</b> <span className="muted">{error.message}</span>{" "}
        <button type="button" onClick={() => this.setState({ error: null })}>Try again</button>
      </div>
    );
  }
}
