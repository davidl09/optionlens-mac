import { Route, Routes } from "react-router-dom";
import { ConnectionBanner, EngineGate, NavigationListener } from "./components/Desktop";
import { Footer, Header } from "./components/Header";
import Alerts from "./pages/Alerts";
import { OptionsBacktest, StockBacktest, StraddleWingBacktest } from "./pages/Backtests";
import { CompareStraddlePage, CompareVolatilityPage } from "./pages/Compare";
import Dashboard from "./pages/Dashboard";
import { AboutPage, NotFound, StatusPage } from "./pages/InfoPages";
import ReportPage from "./pages/ReportPage";
import ReportsIndex from "./pages/ReportsIndex";
import SettingsPage from "./pages/SettingsPage";
import SymbolLayout from "./pages/symbol/SymbolLayout";
import Watchlists from "./pages/Watchlists";

export default function App() {
  return (
    <EngineGate>
    <div className="app">
      <NavigationListener />
      <Header />
      <ConnectionBanner />
      <main className="app-main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/reports" element={<ReportsIndex />} />
          <Route path="/reports/:id" element={<ReportPage />} />
          <Route path="/s/:sym/*" element={<SymbolLayout />} />
          <Route path="/backtest/stock" element={<StockBacktest />} />
          <Route path="/backtest/options" element={<OptionsBacktest />} />
          <Route path="/backtest/straddle-wing" element={<StraddleWingBacktest />} />
          <Route path="/compare/volatility" element={<CompareVolatilityPage />} />
          <Route path="/compare/straddle" element={<CompareStraddlePage />} />
          <Route path="/watchlists" element={<Watchlists />} />
          <Route path="/alerts" element={<Alerts />} />
          <Route path="/status" element={<StatusPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/about" element={<AboutPage />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
      <Footer />
    </div>
    </EngineGate>
  );
}
