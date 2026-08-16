import SwiftUI

/// The app entry point.
///
/// Identical on iOS and macOS, which is what lets the whole UI be compiled and
/// checked here against the macOS SDK — there is no Xcode on this machine, so no
/// iOS SDK and no simulator. Opening this package in Xcode and adding an iOS app
/// target is the remaining step; the views themselves need no changes.
@main
struct HSAVaultApp: App {
    var body: some Scene {
        WindowGroup {
            ReceiptListView()
        }
    }
}
