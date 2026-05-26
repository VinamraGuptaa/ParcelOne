import { useEffect, useState } from 'react';
import { Outlet } from 'react-router-dom';
import Masthead from './Masthead';
import Navbar from './Navbar';
import Sidebar from './Sidebar';
import StatusBar from './StatusBar';
import { apiGet, type WorkflowSummary } from '../../api/client';

export default function AppShell() {
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);

  useEffect(() => {
    apiGet<{ workflows: WorkflowSummary[] }>('/workflows')
      .then((d) => setWorkflows(d.workflows))
      .catch(() => {});
  }, []);

  // Expose a refresh function via a custom event so child pages can trigger sidebar update.
  useEffect(() => {
    function handleRefresh() {
      apiGet<{ workflows: WorkflowSummary[] }>('/workflows')
        .then((d) => setWorkflows(d.workflows))
        .catch(() => {});
    }
    window.addEventListener('icy-disk:refresh-sidebar', handleRefresh);
    return () => window.removeEventListener('icy-disk:refresh-sidebar', handleRefresh);
  }, []);

  return (
    <div className="app-shell">
      <Masthead />
      <Navbar />
      <Sidebar workflows={workflows} />
      <main className="main-content">
        <Outlet />
      </main>
      <StatusBar />
    </div>
  );
}
