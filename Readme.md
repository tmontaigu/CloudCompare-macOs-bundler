# CloudCompare macOs Bundler

Collection of scripts to create distributable version of CloudCompare
on macOs. The scripts are (in order in which they should be run):

- `build-dependencies.py`: builds from _source_ the dependencies used by CloudCompare and its plugins.
- `build-cloudcompare.py`: builds the CloudCompare.app using the previously build dependencies.
- `macdeploycc.py`: makes the targeted CloudCompare.app fully self-contained by moving all external libs
                    used by the app itself and plugins inside the .app bundle.
- `send-for-notarization.py`: sends the target CloudCompare.app on Apple's servsers to get notarized
- `create-dmg.py`: creates the .dmg that can be shared to users (the .app in it must be signed + notarized)


After a .app was sent for notarization, we have to wait for apple response,
if the response is positive, we have to staple the .app before creating the dmg.

`xcrun stapler staple TheApp.app`

validate :
`xcrun stapler validate TheApp.app`



# Misc

|  arch  | min macos version |
|--------|-------------------|
| x86_64 |      10.15        |
| arm64  |      11.0         |
