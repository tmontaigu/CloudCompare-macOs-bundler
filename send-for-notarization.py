#!/usr/bin/env python3

from argparse import ArgumentParser
import subprocess
import plistlib



def main():
	parser = ArgumentParser()
	parser.add_argument("app_bundle_path")
	args = parser.parse_args()

	assert args.app_bundle_path.endswith(".app"), "Must point to a .app"


	with open(f"{args.app_bundle_path}/Contents/Info.plist", mode='rb') as fp:
	    plist_info = plistlib.load(fp)
	    version_string = plist_info['CFBundleShortVersionString']

	file_output = subprocess.run(['file', f'{args.app_bundle_path}/Contents/MacOs/CloudCompare'], capture_output=True).stdout.decode()

	if 'x86_64' in file_output:
	    arch = 'x86_64'
	elif 'arm64' in file_output:
	    arch = 'arm64'
	else:
	    raise SystemExit(f"Could not determine arch from {file_output}")


	zip_path = f"./workdir/{arch}/CloudCompare-{version_string}-{arch}.zip"
	subprocess.run([
		'ditto',
		'-c',
		'-k',
		'-rsrc',
		'--sequesterRsrc',
		'--keepParent',
		args.app_bundle_path,
		zip_path
	], 
		check=True
	)

	apple_email_id = input("Enter AppleID email: ")

	subprocess.run([
		'xcrun',
		'altool',
		'--notarize-app',
		'--primary-bundle-id',
		'org.cloudcompare.cloudcompare',
		'-u',
		apple_email_id,
		'--file',
		zip_path
	])

	

if __name__ == '__main__':
	main()