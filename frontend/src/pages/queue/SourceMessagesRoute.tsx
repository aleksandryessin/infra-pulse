import { ReviewDraftsProvider } from '../../providers/ReviewDraftsProvider';
import { ReplayQueuePage } from './ReplayQueuePage';

/** Исходные сообщения replay/received (прежняя очередь) — отдельный чанк, без пункта меню. */
export default function SourceMessagesRoute() {
  return (
    <ReviewDraftsProvider>
      <ReplayQueuePage />
    </ReviewDraftsProvider>
  );
}
