import Foundation

@main
enum SendButtonSelectionTests {
    static func main() {
        for label in ["전송", "보내기", "Send", " send ", "send_button", "send-message-button"] {
            precondition(SendButtonSelection.matches(labels: [label], supportsPress: true))
        }
        for label in ["예약 전송", "Send later", "전송 옵션", "첨부", "", "메시지 입력"] {
            precondition(!SendButtonSelection.matches(labels: [label], supportsPress: true))
        }
        precondition(!SendButtonSelection.matches(labels: ["전송"], supportsPress: false))
        precondition(SendButtonSelection.matches(labels: ["", "전송"], supportsPress: true))
        print("Send button selection: 14 cases passed")
    }
}
