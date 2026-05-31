import { BrowserRouter, Routes, Route } from 'react-router-dom';
import RequireAuth from './components/auth/RequireAuth';
import AppShell from './components/layout/AppShell';
import { AuthProvider } from './context/AuthContext';
import DashboardPage from './pages/DashboardPage';
import JobReportPage from './pages/JobReportPage';
import LoginPage from './pages/LoginPage';
import NewSearchPage from './pages/NewSearchPage';
import SignUpPage from './pages/SignUpPage';
import WorkflowReportPage from './pages/WorkflowReportPage';

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/signup" element={<SignUpPage />} />
          <Route element={<RequireAuth />}>
            <Route element={<AppShell />}>
              <Route index element={<DashboardPage />} />
              <Route path="search" element={<NewSearchPage />} />
              <Route path="report/workflow/:id" element={<WorkflowReportPage />} />
              <Route path="report/job/:id" element={<JobReportPage />} />
            </Route>
          </Route>
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  );
}
