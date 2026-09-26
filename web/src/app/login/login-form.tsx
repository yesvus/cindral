"use client";

import { useActionState } from "react";
import { AdminLoginScreen } from "@yesvus/helmdeck";
import { signIn } from "./actions";

export function LoginForm({ defaultEmail }: { defaultEmail?: string }) {
  const [error, formAction, pending] = useActionState(signIn, undefined);

  return (
    <AdminLoginScreen
      brandLabel="Cindral"
      homeHref="/"
      busy={pending}
      defaultEmail={defaultEmail}
      errorMessage={error}
      onSubmit={({ email, password }) => {
        const form = new FormData();
        form.set("email", email);
        form.set("password", password);
        formAction(form);
      }}
    />
  );
}
