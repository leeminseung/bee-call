import Foundation
import Intents

/// Tapping a Bee Call entry in the Phone app's Recents opens the app with an INStartCallIntent, in an
/// NSUserActivity of that type. Bee Call has one number to call, so the tap makes a new call, as the Call
/// button does (the owner's decision of 2026-10-15). Info.plist lists the type under NSUserActivityTypes.
enum RecentsCall {
    static let activityType = NSStringFromClass(INStartCallIntent.self)

    /// Signed out, the tap only opens the app on the sign-in screen; during a call it changes nothing.
    static func startsCall(_ activity: NSUserActivity, signedIn: Bool, inCall: Bool) -> Bool {
        activity.activityType == activityType && signedIn && !inCall
    }
}
