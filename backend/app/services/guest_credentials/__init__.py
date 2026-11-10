"""Guest credentials: passwords, local users and SSH public keys inside a running Linux instance.

The dashboard runs fixed scripts in the guest through LXD exec, so a person who has never used a
shell can still make the Serial and Graphics login prompts usable, or let a key in. Everything a
guest sends back is untrusted: a guest user can plant links, fill files and print anything.
"""
