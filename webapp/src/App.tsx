import { useState } from 'react';
import { SplashScreen } from './components/SplashScreen';
import { DashboardLayout } from './components/DashboardLayout';
import type { SectionKey } from './components/TopNav';
import { Home } from './pages/Home';
import { ClaudeCodeGuide } from './pages/ClaudeCodeGuide';
import { DevinGuide } from './pages/DevinGuide';
import { AddDocumentationForm } from './pages/AddDocumentationForm';

export default function App() {
  const [showSplash, setShowSplash] = useState(true);
  const [section, setSection] = useState<SectionKey>('home');

  if (showSplash) {
    return <SplashScreen onDone={() => setShowSplash(false)} />;
  }

  return (
    <DashboardLayout active={section} onChange={setSection}>
      {section === 'home' && <Home />}
      {section === 'claude-code' && <ClaudeCodeGuide />}
      {section === 'devin' && <DevinGuide />}
      {section === 'add-docs' && <AddDocumentationForm />}
    </DashboardLayout>
  );
}
