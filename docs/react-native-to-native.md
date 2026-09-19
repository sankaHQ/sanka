# SPDX-License-Identifier: Apache-2.0
# React Native to native migration (experimental)

`sanka/react-native-to-native` is a Sanka Code extension that captures a React
Native application's screens, navigation, styles, local state, data loading and
forms, and generates native code from that capture. It is developed in
[`sankaHQ/extensions`](https://github.com/sankaHQ/extensions/tree/main/packages/sanka-extension-react-native-to-native)
and is **experimental**: it is not listed in the marketplace catalog, so
`sanka extension add` cannot install it yet. Run it from its built wheel through
the `sanka-extension/v1` subprocess protocol (`sanka-extension-react-native-to-native`)
or from the extensions checkout. Nothing here is a complete app migration or a
store-submission claim.

## Targets

- **`swiftui`**: `scan`, `plan`, `apply`, `test` and `verify`. The plan renders a
  Swift package (typed routes, one state, reducer and view file per screen,
  styles, Codable models, an `APIClient` behind injectable transport and
  storage protocols, a tree-dump executable) and an XcodeGen spec for the iOS
  shell; no `pbxproj` is generated.
- **`compose`**: `scan` and `plan` only until the Jetpack Compose emitter
  lands. The same capture drives both targets.

## Source envelope

Slice 1 (navigation, static screens, local state): React Navigation (native
stack and bottom tabs with literal parameter lists) or Expo Router (the `app/`
tree, groups, one dynamic segment per route, `_layout` Stack/Tabs); default-
exported function components; `useState` with literal initial values; a bounded
element set (`View`, `Text`, `Image`, `Pressable`, `TextInput`, `Switch`,
`FlatList`, `ScrollView`, `SafeAreaView`, `ActivityIndicator`, `StatusBar`,
`Link`, fragments, conditionals, `map` over state); `StyleSheet.create` literals
with a bounded property set; assets referenced by `require`.

Slice 2 (data loading and forms): exact-shape `fetch` loaders with loading and
error flags, `useEffect(..., [])` mount effects, pull-to-refresh, JSON `POST`
submissions followed by `goBack` or a state reset, a bearer token from
`AsyncStorage`, and `AsyncStorage` string reads and writes. Response types are
exported interfaces of scalar fields.

Every screen gets a disposition: `native-screen` or `needs-manual-adaptation`
with stable reason codes (`SANKA_RN_THIRD_PARTY_COMPONENT`, `SANKA_RN_NATIVE_MODULE`,
`SANKA_RN_STATE_LIBRARY`, `SANKA_RN_ANIMATION`, `SANKA_RN_NETWORK`,
`SANKA_RN_STORAGE`, …). Readiness is the share of native screens. Non-native
screens become placeholder views so partial apps still build; `verify` reports
them as failures.

## Structural parity

`test` builds the package with `swift build` and dumps a normalized tree per
screen and scenario with `swift run` (roles `container`, `text`, `button`,
`image`, `textfield`, `switch`, `list`, `listitem`, `indicator`, with text,
labels, enabled and checked state and typed actions such as
`{"navigate": "Detail", "params": {...}}`). `verify` renders the React Native
source screens in Node.js 22 with `react` and `react-test-renderer` against an
in-repository React Native stub, applies the same scenarios (initial, loaded,
load failed, each press, toggle, typed text, submit and failed submit) and
compares canonical JSON trees. Screens with effects need a
`verify-cases.json` under the artifact root that declares fixture responses and
failure variants; both sides consume the same document. Layout, colours and
pixels are not compared.

## Toolchains

- Node.js 20 or later for `scan`, `plan` and `apply`; Node.js 22 for `verify`
  (`SANKA_NODE`), with the pinned `react` and `react-test-renderer` toolset.
- Swift 6 for `test` and `verify` on macOS; the macOS destination builds with the
  Command Line Tools alone (`DEVELOPER_DIR=/Library/Developer/CommandLineTools`),
  the iOS simulator destination needs a licensed Xcode, and `App/project.yml`
  needs XcodeGen.

## Acceptance

The workspace plan in `sanka-project/plan/sanka-code-paths/react-native-to-native.md`
tracks milestones; the extension's macOS CI job runs the gated Swift and Node.js
suites on every change. The Compose emitter, the hosted
`react-native-to-swiftui` recipe and the Bench lanes are planned but not shipped.
