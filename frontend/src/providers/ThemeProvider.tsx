import { ConfigProvider } from 'antd';
import ruRU from 'antd/locale/ru_RU';
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { lightTheme, darkTheme } from '../styles/theme';

export type Mode = 'light' | 'dark';

const STORAGE_KEY = 'theme-mode';

interface ModeContextValue {
  mode: Mode;
  toggle: () => void;
}

const ModeCtx = createContext<ModeContextValue>({
  mode: 'light',
  toggle: () => {},
});

export const useThemeMode = () => useContext(ModeCtx);

function getInitialMode(): Mode {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === 'light' || saved === 'dark') return saved;
  } catch {
    /* localStorage может быть недоступен */
  }
  return window.matchMedia('(prefers-color-scheme: dark)').matches
    ? 'dark'
    : 'light';
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<Mode>(getInitialMode);

  const toggle = useCallback(
    () => setMode((m) => (m === 'light' ? 'dark' : 'light')),
    [],
  );

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, mode);
    } catch {
      /* ignore */
    }
    // для CSS-файлов и нативных элементов (скроллбары, инпуты)
    document.documentElement.dataset.theme = mode;
    document.documentElement.style.colorScheme = mode;
  }, [mode]);

  const value = useMemo(() => ({ mode, toggle }), [mode, toggle]);

  return (
    <ModeCtx.Provider value={value}>
      <ConfigProvider
        locale={ruRU}
        theme={mode === 'light' ? lightTheme : darkTheme}
      >
        {children}
      </ConfigProvider>
    </ModeCtx.Provider>
  );
}
