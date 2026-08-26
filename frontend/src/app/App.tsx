import { BrowserRouter } from 'react-router-dom';
import { Toaster } from 'sonner';

import { TooltipProvider } from '@/components/ui/tooltip';
import { LocaleProvider } from '@/i18n';

import { AppRoutes } from './routes';
import { SessionProvider } from './session';

/**
 * Providers, outermost first.
 *
 * Locale wraps Session because a sign-in failure has to be readable before there
 * is a session to read it with -- the error text on that screen comes from the
 * dictionary, and a provider ordering that put Session first would render the
 * raw key.
 */
export function App() {
  return (
    <LocaleProvider>
      <SessionProvider>
        <TooltipProvider delayDuration={300}>
          <BrowserRouter>
            <AppRoutes />
          </BrowserRouter>
          {/* `richColors` so an error is red without every call site saying so. */}
          <Toaster position="bottom-center" richColors closeButton />
        </TooltipProvider>
      </SessionProvider>
    </LocaleProvider>
  );
}
