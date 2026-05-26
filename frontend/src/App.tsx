import { BrowserRouter, Routes, Route } from 'react-router-dom';
import AppShell from './components/layout/AppShell';
import DashboardPage from './pages/DashboardPage';
import NewSearchPage from './pages/NewSearchPage';
import WorkflowReportPage from './pages/WorkflowReportPage';
import JobReportPage from './pages/JobReportPage';

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<DashboardPage />} />
          <Route path="search" element={<NewSearchPage />} />
          <Route path="report/workflow/:id" element={<WorkflowReportPage />} />
          <Route path="report/job/:id" element={<JobReportPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
