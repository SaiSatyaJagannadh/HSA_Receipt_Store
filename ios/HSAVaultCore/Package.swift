// swift-tools-version: 5.9
import PackageDescription

// Deliberately pure Swift: no SwiftUI, no UIKit, no Google SDK. That keeps the
// balance rule buildable and checkable with the Command Line Tools alone, so it
// is verified before anyone installs Xcode or writes a single view.
//
// The checks are an executable rather than a test target on purpose: XCTest and
// swift-testing both ship with Xcode, and neither is present in a bare CLT
// install. `swift run HSAVaultCoreChecks` works anywhere Swift does. Convert it
// to XCTest once Xcode is on the machine.
let package = Package(
    name: "HSAVaultCore",
    platforms: [.iOS(.v16), .macOS(.v13)],
    products: [
        .library(name: "HSAVaultCore", targets: ["HSAVaultCore"])
    ],
    targets: [
        .target(name: "HSAVaultCore"),
        .executableTarget(name: "HSAVaultCoreChecks", dependencies: ["HSAVaultCore"]),
        // The screens. SwiftUI is identical on iOS and macOS for everything used
        // here, so building for the macOS host compiles the same view code an
        // iOS target would — which is how the UI is verified without an iOS SDK.
        .executableTarget(name: "HSAVaultApp", dependencies: ["HSAVaultCore"]),
    ]
)
