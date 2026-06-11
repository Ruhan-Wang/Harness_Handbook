import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';
import './index.css';

// Apply persisted theme before first paint (default dark).
const theme = localStorage.getItem('hs-theme') || 'dark';
document.documentElement.classList.toggle('dark', theme !== 'light');

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
