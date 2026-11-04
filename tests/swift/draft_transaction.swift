import Foundation

// Compile with BackgroundDraftTransaction.swift; no KakaoTalk access is needed.
enum AutomationError: Error { case sendFailed(String) }

@main
enum DraftTransactionTests {
    static func main() throws {
        var text = "기존 초안"
        var sent = ""
        try BackgroundDraftTransaction.replaceAndDispatch(
            message: "새 메시지", read: { text }, write: { text = $0; return true },
            dispatch: { sent = text; text = "" })
        precondition(sent == "새 메시지" && text.isEmpty)

        text = "기존 초안"
        do {
            try BackgroundDraftTransaction.replaceAndDispatch(
                message: "새 메시지", read: { text }, write: { text = $0; return true },
                dispatch: { throw AutomationError.sendFailed("target_changed") })
            fatalError("Expected failure")
        } catch {}
        precondition(text == "기존 초안")

        do {
            try BackgroundDraftTransaction.replaceAndDispatch(
                message: "새 메시지", read: { text }, write: { text = $0; return true },
                dispatch: { text = "사용자가 바꾼 초안"; throw AutomationError.sendFailed("changed") })
            fatalError("Expected failure")
        } catch {}
        precondition(text == "사용자가 바꾼 초안")

        text = "기존 초안"
        do {
            try BackgroundDraftTransaction.replaceAndDispatch(
                message: "새 메시지", read: { text },
                write: { text = $0; return $0 != "새 메시지" },
                dispatch: { fatalError("Must not send after failed write") })
            fatalError("Expected failure")
        } catch {}
        precondition(text == "기존 초안")

        do {
            try BackgroundDraftTransaction.replaceAndDispatch(
                message: "새 메시지", read: { nil },
                write: { _ in fatalError("Must not overwrite unreadable draft") },
                dispatch: { fatalError("Must not send unreadable draft") })
            fatalError("Expected failure")
        } catch {}

        text = ""
        try BackgroundDraftTransaction.replaceAndDispatch(
            message: "새 메시지", read: { text }, write: { text = $0; return true },
            dispatch: {})
        // Once Return was dispatched, leave uncertain delivery for manual review.
        precondition(text == "새 메시지")
        print("Draft transaction: 6 cases passed")
    }
}
