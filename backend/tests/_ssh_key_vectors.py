"""Public keys made with OpenSSH 9.6 `ssh-keygen` for these tests, with what `ssh-keygen -lf` printed.

Public halves only; their private keys were discarded. `sk` is built by hand from a fixed 32-byte
point and the `ssh:` application, as a FIDO key's public half is laid out.
"""

VECTORS = {
    "ed25519": ('ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDdWxLrYEC6VCx4KnfAxUh0W+5zn5lruMRUNnO6ua5IS qa ed25519', 256, 'SHA256:lja0vtFZEJFy78kVVFZVM4A1sL2Lhwec1uLMysk3j/o'),
    "ecdsa256": ('ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBAYoz7DKKr2ly+p0EYA4YJUkIbvD1/Wl3I7CHcFcuuEknh/E9yySBsLwd6sd5cObd5Ai3X4Dn310Tp0Rszhloso= qa ecdsa256', 256, 'SHA256:z4a7CX9kh+R8llvm5NHO+eafoqL6Ae60gF3CYD2tiDA'),
    "ecdsa384": ('ecdsa-sha2-nistp384 AAAAE2VjZHNhLXNoYTItbmlzdHAzODQAAAAIbmlzdHAzODQAAABhBPMbFhfepfSRzZdK+mDAqCXjc/+jSjcEhMqUtAOYJ5v4cAGtvnOR69jkVnBWMfaawKVUsMjjWBsUdoxqcupaQiJ3PUAUynDn0UgqD+X7z0y7yI+vqgUNSLejJ4DqU/ZOYQ== qa ecdsa384', 384, 'SHA256:XTgttY1R9+Tul/ZwyLsxohJK0KP8hDCzQPrpg89zIgo'),
    "ecdsa521": ('ecdsa-sha2-nistp521 AAAAE2VjZHNhLXNoYTItbmlzdHA1MjEAAAAIbmlzdHA1MjEAAACFBAELf1u5lzoz3x+YmZ8ATBpZv6JFBVBZK+ZgpiMbHgaE6umKuXY8fS4edo1+3mXgKP7B8aXOR4n9ksy5GIym26KntwBQfBWDrkcgxeJXfUYySb6yGC1Aqejc5iwMW/aWtKlC53hw8gbY8wHUAdUSiDh4sogB/BE5X4zj3avy++SNvC08qQ== qa ecdsa521', 521, 'SHA256:dDrFJbqm0ma9SgYG5rLF4kuf2vQkicKEo/cgzuhDTnE'),
    "rsa2048": ('ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDZ58DGeLj+lyfiFFCk72jK/e5A0J6g7RTz2xFsscGQD+sjPZ57CEC1Zf8HKr68f/Hg7NHSvkNOEthdcFH2BZmQ5Keg689Efny92cqR2LCfaJMwe43MdBoCCKLSFd3qG+ZxsykCja8cfarMogzG+5dzFG2KH+ExfEkSAOGfumh5OrlaZeZdxLRDUnGjZATLiqfOT1+gKAutmzX/svBheWPcq25IL2mOdpSjZsrfLXTbNVe22TWszYMPf7lvg1Q0mEk3pmJObi1bGAsykpDFHx1q20f4Kv4nhoTIphK9z7EC3SsG2GiAXnuiM58wtg5dBcUObrn1XXidKjxl7n8vjCo/ qa rsa2048', 2048, 'SHA256:I6Pnid1U76SsK9pq6Z3SQthtylhHjNKhMwGzskLkjfE'),
    "rsa3072": ('ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQC6H1Tm/OoWfI/fYlf7/dDJbkDiHElp+P6QSilhR8yKrfniysXXpwBVJFJxLIJN4SHI2i/aN0lN1cAYJC4LezRZSoYu6aAdpW8yZf4pFnwdH33mYDi7Qjy9CTnqMj+ygPdygiEYFcHURJOkbR6xt/GnsbfK94cwshnkfAEoEXX4QrOr+0I73In1LpH68h8KyBObwGi0UVP3RpvqCzyGuCFPRGqVTS+6ibff2dGjLmNcfbwWjy3NSWlnuFzelIhoQu5cEbOg7OJp/0aERpJSMmHIaxsul8hcQcBxkEVCppiNexx3wQQNfoOI8tn8f5bNaRevIhZM+j0W9HNavhErVdGSVXEvhRB2XG/fXinE6maPWBnUtgFNEBL7Z29poZ4FrSQlpf+wmIE65XHj6WwUhqU8RNNLREB1biB713v3xE8pbJ394pib86OqJ3fO5RpKJAQVDMtBZfkbViGclY2JHbvRgaU1u/NZ+zGiWhP4DRopmSkrMxcMGw2ZtZ5yg/friKs= qa rsa3072', 3072, 'SHA256:RPch6MR0Ob5WUmvc/5KYRmGO49u4bB4pAKxvyMv9dEc'),
    "rsa1024": ('ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAAAgQCdLrrn6mdxuwV8DDgKOuacj5cuJDwC0lwPDAPp2zv1hRLsMgObiLCNV8QQ7GsL7ITmMgjYL1/cRQaYbJJMLxQ97EvYA2Gn0bhxYZTK/st1REEFGRxbC24a3UIfrQMw+i3tsanlbg9GseqopDrAcpOEBR8ChDFZ8WlHHfmpFudSDQ== qa rsa1024', 1024, 'SHA256:DZHtpICF0NqNG4GO65K/h1jb3fGz5tHQMwA225mpKS8'),
    "sk": ("sk-ssh-ed25519@openssh.com AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29tAAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4fAAAABHNzaDo= qa sk", 256, "SHA256:/p0CbeE3dk2SyW1OXXsThGc12ezDVD8eGw2/vtztDfk"),
}
