import { createContext, useCallback, useContext, useState, type PropsWithChildren } from 'react';
import type { ReviewNoteCreate } from '../api/attention';

export interface ReviewDraft {
  actionText: string;
  resultText: string;
  reasonText: string;
  pendingPayload?: ReviewNoteCreate;
}

export const EMPTY_REVIEW_DRAFT: ReviewDraft = {
  actionText: '',
  resultText: '',
  reasonText: '',
};

interface ReviewDraftsContextValue {
  drafts: Record<string, ReviewDraft>;
  setDraft: (key: string, draft: ReviewDraft) => void;
}

const ReviewDraftsContext = createContext<ReviewDraftsContextValue | null>(null);

export function ReviewDraftsProvider({ children }: PropsWithChildren) {
  const [drafts, setDrafts] = useState<Record<string, ReviewDraft>>({});
  const setDraft = useCallback((key: string, draft: ReviewDraft) => {
    setDrafts((current) => {
      const next = { ...current };
      if (!draft.actionText && !draft.resultText && !draft.reasonText && !draft.pendingPayload) {
        delete next[key];
      } else {
        next[key] = draft;
      }
      return next;
    });
  }, []);

  return <ReviewDraftsContext.Provider value={{ drafts, setDraft }}>{children}</ReviewDraftsContext.Provider>;
}

export function useReviewDrafts(): ReviewDraftsContextValue {
  const context = useContext(ReviewDraftsContext);
  if (!context) throw new Error('ReviewDraftsProvider is required');
  return context;
}
